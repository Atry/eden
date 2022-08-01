#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.


import hashlib
import os
import re
import subprocess
import tempfile


dirname = os.path.dirname

crate_root = dirname(dirname(os.path.realpath(__file__)))


def expand_parser(pest):
    """expand the "#[derive(Parser)] part"""
    with tempfile.TemporaryDirectory() as tmp_root:
        # Copy Cargo.toml, without [dev-dependencies] and [[bench]]
        with open(os.path.join(tmp_root, "Cargo.toml"), "w") as f:
            content = open(os.path.join(crate_root, "Cargo.toml")).read()
            content = content.split("[dev-dependencies]")[0]
            f.write(content)

        # Copy spec.pest
        os.mkdir(os.path.join(tmp_root, "src"))
        with open(os.path.join(tmp_root, "src", "spec.pest"), "wb") as f:
            f.write(pest)

        # Create a minimal project which is used to expand ConfigParser
        with open(os.path.join(tmp_root, "src", "lib.rs"), "w") as f:
            f.write(
                """
#[derive(Parser)]
#[grammar = "spec.pest"]
pub(crate) struct ConfigParser;
"""
            )

        # Run cargo-expand
        env = os.environ.copy()
        env["RUSTFMT"] = "false"
        expanded = subprocess.check_output(
            ["cargo-expand", "--release"], env=env, cwd=tmp_root
        )
        expanded = expanded.decode("utf-8")

        # Keep only interesting parts
        rule_struct = re.search(
            r"^pub enum Rule [^}]*^\}", expanded, re.S + re.M
        ).group(0)
        parser_impl = re.search(
            r"^impl ::pest::Parser<Rule> for ConfigParser .*^\}", expanded, re.S + re.M
        ).group(0)

        code = f"""
#[allow(dead_code, non_camel_case_types)]
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
{rule_struct}

pub(crate) struct ConfigParser;

{parser_impl}
"""

        return code


def write_generated_parser():
    spec_pest_path = os.path.join(crate_root, "src", "spec.pest")
    with open(spec_pest_path, "rb") as f:
        spec = f.read()

    checksum = hashlib.sha1(spec).hexdigest()
    output_path = os.path.join(crate_root, "src", "parser.rs")

    try:
        with open(output_path) as f:
            old_checksum = re.search(r"pest-checksum: (.*)\.", f.read()).group(1)
        if old_checksum == checksum:
            print(
                "No need to update %s because %s is not changed."
                % (output_path, spec_pest_path)
            )
            return
    except Exception:
        pass

    with open(output_path, "w") as f:
        code = expand_parser(spec)
        f.write(
            f"""
// Generated by generate_parser.py. Do not edit manually. Instead, edit
// spec.pest, then run generate_parser.py (require cargo-expand).
//
// This file should really be just 3 lines:
//
// #[derive(Parser)]
// #[grammar = "spec.pest"]
// pub(crate) struct ConfigParser;
//
// However, `#[grammar = "spec.pest"]` does not play well with Buck build,
// because pest_derive cannot find "spec.pest" in buck build environment.
// Therefore this file is {'detareneg@'[::-1]}. {"tnil-on@"[::-1]}.
// pest-checksum: {checksum}.

{code}"""
        )


if __name__ == "__main__":
    write_generated_parser()