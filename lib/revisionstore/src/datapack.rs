// Copyright Facebook, Inc. 2018
//! Classes for constructing and serializing a datapack file and index.
//!
//! A datapack is a pair of files that contain the revision contents for various
//! file revisions in Mercurial. It contains only revision contents (like file
//! contents), not any history information.
//!
//! It consists of two files, with the following format. All bytes are in
//! network byte order (big endian).
//!
//! ```text
//!
//! .datapack
//!     The pack itself is a series of revision deltas with some basic header
//!     information on each. A revision delta may be a fulltext, represented by
//!     a deltabasenode equal to the nullid.
//!
//!     datapack = <version: 1 byte>
//!                [<revision>,...]
//!     revision = <filename len: 2 byte unsigned int>
//!                <filename>
//!                <node: 20 byte>
//!                <deltabasenode: 20 byte>
//!                <delta len: 8 byte unsigned int>
//!                <delta>
//!                <metadata-list len: 4 byte unsigned int> [1]
//!                <metadata-list>                          [1]
//!     metadata-list = [<metadata-item>, ...]
//!     metadata-item = <metadata-key: 1 byte>
//!                     <metadata-value len: 2 byte unsigned>
//!                     <metadata-value>
//!
//!     metadata-key could be METAKEYFLAG or METAKEYSIZE or other single byte
//!     value in the future.
//!
//! .dataidx
//!     The index file consists of two parts, the fanout and the index.
//!
//!     The index is a list of index entries, sorted by node (one per revision
//!     in the pack). Each entry has:
//!
//!     - node (The 20 byte node of the entry; i.e. the commit hash, file node
//!             hash, etc)
//!     - deltabase index offset (The location in the index of the deltabase for
//!                               this entry. The deltabase is the next delta in
//!                               the chain, with the chain eventually
//!                               terminating in a full-text, represented by a
//!                               deltabase offset of -1. This lets us compute
//!                               delta chains from the index, then do
//!                               sequential reads from the pack if the revision
//!                               are nearby on disk.)
//!     - pack entry offset (The location of this entry in the datapack)
//!     - pack content size (The on-disk length of this entry's pack data)
//!
//!     The fanout is a quick lookup table to reduce the number of steps for
//!     bisecting the index. It is a series of 4 byte pointers to positions
//!     within the index. It has 2^16 entries, which corresponds to hash
//!     prefixes [0000, 0001,..., FFFE, FFFF]. Example: the pointer in slot
//!     4F0A points to the index position of the first revision whose node
//!     starts with 4F0A. This saves log(2^16)=16 bisect steps.
//!
//!     dataidx = <version: 1 byte>
//!               <config: 1 byte>
//!               <fanouttable>
//!               <index>
//!     fanouttable = [<index offset: 4 byte unsigned int>,...] (2^8 or 2^16 entries)
//!     index = [<index entry>,...]
//!     indexentry = <node: 20 byte>
//!                  <deltabase location: 4 byte signed int>
//!                  <pack entry offset: 8 byte unsigned int>
//!                  <pack entry size: 8 byte unsigned int>
//!
//! ```
//! [1]: new in version 1.
use byteorder::{BigEndian, ReadBytesExt};
use lz4_pyframe::decompress;
use memmap::{Mmap, MmapOptions};
use std::{fmt, result};
use std::cell::RefCell;
use std::fs::File;
use std::io::{Cursor, Read};
use std::path::Path;
use std::rc::Rc;

use dataindex::DataIndex;
use datastore::{DataStore, Delta, Metadata};
use error::Result;
use key::Key;
use node::Node;

#[derive(Debug, Fail)]
#[fail(display = "Datapack Error: {:?}", _0)]
struct DataPackError(String);

pub struct DataPack {
    mmap: Mmap,
    version: u8,
    index: DataIndex,
}

pub struct DataEntry<'a> {
    offset: u64,
    filename: &'a [u8],
    node: Node,
    delta_base: Node,
    compressed_data: &'a [u8],
    data: RefCell<Option<Rc<[u8]>>>,
    metadata: Metadata,
    next_offset: u64,
}

impl<'a> DataEntry<'a> {
    pub fn new(buf: &'a [u8], offset: u64, version: u8) -> Result<Self> {
        let mut cur = Cursor::new(buf);
        cur.set_position(offset);

        // Filename
        let filename_len = cur.read_u16::<BigEndian>()? as u64;
        let filename = &buf.get(cur.position() as usize..(cur.position() + filename_len) as usize)
            .ok_or(DataPackError(format!(
                "buffer (length {:?}) not long enough to read filename (length {:?})",
                buf.len(),
                filename_len
            )))?;
        let cur_pos = cur.position();
        cur.set_position(cur_pos + filename_len);

        // Node
        let mut node_buf: [u8; 20] = Default::default();
        cur.read_exact(&mut node_buf)?;
        let node = Node::from(&node_buf);

        // Delta
        cur.read_exact(&mut node_buf)?;
        let delta_base = Node::from(&node_buf);

        let delta_len = cur.read_u64::<BigEndian>()?;
        let compressed_data = &buf.get(
            cur.position() as usize..(cur.position() + delta_len) as usize,
        ).ok_or(DataPackError(format!(
            "buffer (length {:?}) not long enough to read data (length {:?})",
            buf.len(),
            delta_len
        )))?;
        let data = RefCell::new(None);
        let cur_pos = cur.position();
        cur.set_position(cur_pos + delta_len);

        // Metadata
        let mut metadata = Metadata {
            flags: None,
            size: None,
        };
        if version == 1 {
            metadata = Metadata::read(&mut cur)?;
        }

        let next_offset = cur.position();

        Ok(DataEntry {
            offset,
            filename,
            node,
            delta_base,
            compressed_data,
            data,
            metadata,
            next_offset,
        })
    }

    pub fn offset(&self) -> u64 {
        self.offset
    }

    pub fn filename(&self) -> &[u8] {
        self.filename
    }

    pub fn node(&self) -> &Node {
        &self.node
    }

    pub fn delta_base(&self) -> &Node {
        &self.delta_base
    }

    pub fn delta(&self) -> Result<Rc<[u8]>> {
        let mut cell = self.data.borrow_mut();
        if cell.is_none() {
            *cell = Some(Rc::<[u8]>::from(decompress(&self.compressed_data)?));
        }

        Ok(Rc::clone(cell.as_ref().unwrap()))
    }

    pub fn metadata(&self) -> &Metadata {
        &self.metadata
    }
}

impl<'a> fmt::Debug for DataEntry<'a> {
    fn fmt(&self, f: &mut fmt::Formatter) -> result::Result<(), fmt::Error> {
        let delta = self.delta()
            .unwrap_or_else(|e| Rc::from(format!("{:?}", e).as_ref()));
        write!(
            f,
            "DataEntry {{\n  offset: {:?}\n  filename: {:?}\n  \
             node: {:?}\n  delta_base: {:?}\n  compressed_len: {:?}\n  \
             data_len: {:?}\n  data: {:?}\n  metadata: N/A\n}}",
            self.offset,
            self.filename,
            self.node,
            self.delta_base,
            self.compressed_data.len(),
            delta.len(),
            delta.iter().map(|b| *b as char).collect::<String>(),
        )
    }
}

impl DataPack {
    pub fn new(path: &Path) -> Result<Self> {
        let path = path.with_extension("datapack");
        let file = File::open(&path)?;
        let len = file.metadata()?.len();
        if len < 1 {
            return Err(format_err!(
                "empty datapack '{:?}' is invalid",
                path.to_str().unwrap_or("<unknown>")
            ));
        }

        let mmap = unsafe { MmapOptions::new().len(len as usize).map(&file)? };
        let version = mmap[0];
        let index_path = path.with_extension("dataidx");
        Ok(DataPack {
            mmap: mmap,
            version: version,
            index: DataIndex::new(&index_path)?,
        })
    }

    pub fn len(&self) -> usize {
        self.mmap.len()
    }

    pub fn read_entry(&self, offset: u64) -> Result<DataEntry> {
        DataEntry::new(self.mmap.as_ref(), offset, self.version)
    }
}

impl DataStore for DataPack {
    fn get(&self, key: &Key) -> Result<Vec<u8>> {
        unimplemented!();
    }
    fn get_delta_chain(&self, key: &Key) -> Result<Vec<Delta>> {
        unimplemented!();
    }

    fn get_meta(&self, key: &Key) -> Result<Metadata> {
        let index_entry = self.index.get_entry(key.node())?;
        Ok(self.read_entry(index_entry.pack_entry_offset)?.metadata)
    }

    fn get_missing(&self, keys: &[Key]) -> Result<Vec<Key>> {
        Ok(keys.iter()
            .filter(|k| self.index.get_entry(k.node()).is_err())
            .map(|k| k.clone())
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use datastore::{Delta, Metadata};
    use mutabledatapack::MutableDataPack;
    use node::Node;
    use tempfile::TempDir;

    fn make_pack(tempdir: &TempDir, deltas: &Vec<(Delta, Option<Metadata>)>) -> DataPack {
        let mut mutdatapack = MutableDataPack::new(tempdir.path(), 1).unwrap();
        for &(ref delta, ref metadata) in deltas.iter() {
            mutdatapack.add(&delta, metadata.clone()).unwrap();
        }

        let path = mutdatapack.close().unwrap();

        DataPack::new(&path).unwrap()
    }

    #[test]
    fn test_empty() {
        let tempdir = TempDir::new().unwrap();
        let pack = make_pack(&tempdir, &vec![]);
        assert!(pack.len() > 0);
    }

    #[test]
    fn test_get_missing() {
        let tempdir = TempDir::new().unwrap();
        let revisions = vec![
            (
                Delta {
                    data: Rc::new([1, 2, 3, 4]),
                    base: Key::new(Box::new([0]), Node::random()),
                    key: Key::new(Box::new([0]), Node::random()),
                },
                None,
            ),
        ];
        let pack = make_pack(&tempdir, &revisions);
        for &(ref delta, ref metadata) in revisions.iter() {
            let missing = pack.get_missing(&[delta.key.clone()]).unwrap();
            assert_eq!(missing.len(), 0);
        }

        let not = Key::new(Box::new([1]), Node::random());
        let missing = pack.get_missing(&vec![not.clone()]).unwrap();
        assert_eq!(missing, vec![not.clone()]);
    }

    #[test]
    fn test_get_meta() {
        let tempdir = TempDir::new().unwrap();
        let revisions = vec![
            (
                Delta {
                    data: Rc::new([1, 2, 3, 4]),
                    base: Key::new(Box::new([0]), Node::random()),
                    key: Key::new(Box::new([0]), Node::random()),
                },
                None,
            ),
            (
                Delta {
                    data: Rc::new([1, 2, 3, 4]),
                    base: Key::new(Box::new([0]), Node::random()),
                    key: Key::new(Box::new([0]), Node::random()),
                },
                Some(Metadata {
                    size: Some(1000),
                    flags: Some(7),
                }),
            ),
        ];

        let pack = make_pack(&tempdir, &revisions);
        for &(ref delta, ref metadata) in revisions.iter() {
            let meta = pack.get_meta(&delta.key).unwrap();
            let mut metadata = match metadata {
                &Some(ref m) => m.clone(),
                &None => Default::default(),
            };
            assert_eq!(meta, metadata);
        }
    }
}
