# Copyright 2018 Facebook, Inc.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

from __future__ import absolute_import
import itertools
import re
import socket
import time

from mercurial.i18n import _

from mercurial import (
    commands,
    node,
    obsolete,
    registrar,
)

from . import (
    service,
    state,
)

cmdtable = {}
command = registrar.command(cmdtable)

@command('cloudsync')
def cloudsync(ui, repo, **opts):
    """Synchronize commits with the commit cloud service"""

    start = time.time()
    serv = service.get(ui)
    lastsyncstate = state.SyncState(repo)
    cloudrefs = serv.getreferences(lastsyncstate.version)

    synced = False
    while not synced:
        if cloudrefs.version != lastsyncstate.version:
            _applycloudchanges(ui, repo, lastsyncstate, cloudrefs)

        localheads = _getheads(repo)
        localbookmarks = _getbookmarks(repo)

        if (set(localheads) == set(lastsyncstate.heads) and
                localbookmarks == lastsyncstate.bookmarks and
                lastsyncstate.version != 0):
            synced = True

        if not synced:
            # The local repo has changed.  We must send these changes to the
            # cloud.
            obsmarkers = []
            if repo.svfs.exists('commitcloudpendingobsmarkers'):
                with repo.svfs.open('commitcloudpendingobsmarkers') as f:
                    _version, obsmarkers = obsolete._readmarkers(f.read())
            synced, cloudrefs = serv.updatereferences(
                    lastsyncstate.version, lastsyncstate.heads, localheads,
                    lastsyncstate.bookmarks, localbookmarks, obsmarkers)
            if synced:
                lastsyncstate.update(cloudrefs.version, localheads,
                                     localbookmarks)
                if obsmarkers:
                    repo.svfs.unlink('commitcloudpendingobsmarkers')

    elapsed = time.time() - start
    ui.debug(
        "%s cloudsync is done in %0.2f sec\n" % (
            ui.label('#commitcloud', 'commitcloud.hashtag'),
            elapsed)
    )

def _applycloudchanges(ui, repo, lastsyncstate, cloudrefs):
    pullcmd, pullopts = _getcommandandoptions('^pull')

    # Pull all the new heads
    pullopts['rev'] = cloudrefs.heads
    pullcmd(ui, repo, **pullopts)

    # Merge cloud bookmarks into the repo
    _mergebookmarks(ui, repo, cloudrefs.bookmarks, lastsyncstate.bookmarks)

    # Merge obsmarkers
    _mergeobsmarkers(ui, repo, cloudrefs.obsmarkers)

    # We have now synced the repo to the cloud version.  Store this.
    lastsyncstate.update(cloudrefs.version, cloudrefs.heads,
                         cloudrefs.bookmarks)

def _mergebookmarks(ui, repo, cloudbookmarks, lastsyncbookmarks):
    localbookmarks = _getbookmarks(repo)
    with repo.wlock(), repo.lock(), repo.transaction('bookmark') as tr:
        changes = []
        allnames = set(localbookmarks.keys() + cloudbookmarks.keys())
        newnames = set()
        for name in allnames:
            localnode = localbookmarks.get(name)
            cloudnode = cloudbookmarks.get(name)
            lastnode = lastsyncbookmarks.get(name)
            if cloudnode != localnode:
                if (localnode is not None and cloudnode is not None and
                        localnode != lastnode and cloudnode != lastnode):
                    # Changed both locally and remotely, fork the local
                    # bookmark
                    forkname = _forkname(ui, name, allnames | newnames)
                    newnames.add(forkname)
                    changes.append((forkname, node.bin(localnode)))
                    ui.warn(_('%s changed locally and remotely, '
                              'local bookmark renamed to %s\n') %
                              (name, forkname))

                if cloudnode != lastnode:
                    if cloudnode is not None:
                        if cloudnode in repo:
                            changes.append((name, node.bin(cloudnode)))
                        else:
                            ui.warn(_('%s not found, '
                                      'not creating %s bookmark\n') %
                                      (cloudnode, name))
                    else:
                        if localnode is not None and localnode != lastnode:
                            # Moved locally, deleted in the cloud, resurrect
                            # at the new location
                            pass
                        else:
                            changes.append((name, None))
        repo._bookmarks.applychanges(repo, tr, changes)

def _mergeobsmarkers(ui, repo, obsmarkers):
    with repo.wlock(), repo.lock(), repo.transaction('commitcloud-obs') as tr:
        tr._commitcloudskippendingobsmarkers = True
        repo.obsstore.add(tr, obsmarkers)

def _forkname(ui, name, othernames):
    hostname = ui.config('commitcloud', 'hostname', socket.gethostname())

    # Strip off any old suffix.
    m = re.match('-%s(-[0-9]*)?$' % re.escape(hostname), name)
    if m:
        suffix = '-%s%s' % (hostname, m.group(1) or '')
        name = name[0:-len(suffix)]

    # Find a new name.
    for n in itertools.count():
        candidate = '%s-%s%s' % (name, hostname, '-%s' % n if n != 0 else '')
        if candidate not in othernames:
            return candidate

def _getheads(repo):
    headsrevset = repo.set('heads(draft()) & not obsolete()')
    return [ctx.hex() for ctx in headsrevset]

def _getbookmarks(repo):
    return {n: node.hex(v) for n, v in repo._bookmarks.items()}

def _getcommandandoptions(command):
    cmd = commands.table[command][0]
    opts = dict(opt[1:3] for opt in commands.table[command][1])
    return cmd, opts
