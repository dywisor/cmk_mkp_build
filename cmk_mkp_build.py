#!/usr/bin/python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import typing
from typing import Any, Optional
from collections.abc import Iterator

import argparse
import io
import json
import os
import pprint
import re
import stat
import sys
import tarfile
import time

from dataclasses import dataclass, field

try:
    import yaml
except ModuleNotFoundError:
    HAVE_YAML = False
else:
    HAVE_YAML = True


class Globals:
    DEFAULT_CMK_VERSION = "2.0.3p22"

    # mkp_info_template : key => (is required, default value)
    mkp_info_template = {
        "author": (False, ""),
        "description": (False, ""),
        "download_url": (False, ""),
        # "files": (False, None),  # will be set during finalize_mkp_info()
        "name": (True, ""),
        "title": (True, ""),
        "version": (True, ""),
        "version.min_required": (True, DEFAULT_CMK_VERSION),
        "version.packaged": (True, DEFAULT_CMK_VERSION),
        "version.usable_until": (False, None),
    }

    @classmethod
    def iter_mkp_info_template_args(cls) -> Iterator[tuple[str, str]]:
        for name in sorted(Globals.mkp_info_template):
            arg_name = "mkp_info_{}".format(name.replace(".", "_"))
            yield (name, arg_name)


@dataclass
class FileInfo:
    path: str
    relpath: str
    name: str
    stat_info: os.stat_result


@dataclass
class FilesTree(object):
    info: FileInfo
    directories: dict[str, "FilesTree"] = field(init=False, default_factory=dict)
    files: dict[str, FileInfo] = field(init=False, default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.files or self.directories)

    def add_directory_node(self, node: "FilesTree") -> None:
        self.directories[node.info.name] = node

    def add_file(self, info: FileInfo) -> None:
        self.files[info.name] = info

    def _dfs_iter(self, depth) -> Iterator["FilesTree"]:
        subdir_depth = depth + 1

        yield (depth, self)

        for subdir_node in self.directories.values():
            yield from subdir_node._dfs_iter(subdir_depth)

    def __iter__(self) -> Iterator[FileInfo]:
        for _, node in self._dfs_iter(0):
            yield node.info
            yield from node.files.values()


def get_argument_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)

    parser.add_argument("plugin_directory", help="plugin source directory")

    parser.add_argument(
        "-o",
        "--outfile",
        metavar="<outfile>",
        dest="outfile",
        default=None,
        help="output file",
    )

    parser.add_argument(
        "--show-info",
        dest="show_info",
        default=False,
        action="store_true",
        help="print generated info to stdout (in addition to creating the mkp file)",
    )

    return parser


def main(prog: str, argv: list[str]) -> None | int | bool:
    arg_parser = get_argument_parser(prog)
    arg_config = arg_parser.parse_args(argv)

    timestamp = time.time()

    plugin_source = scan_plugin_source_directory(arg_config.plugin_directory)

    mkp_info = prepare_mkp_info(arg_config, plugin_source)

    plugin_name = mkp_info["name"]

    cmk_addon_files_tar_name = "cmk_addon_files.tar"

    cmk_addon_files_list, cmk_addon_files_tar = build_cmk_addon_files_tar(
        plugin_source, plugin_name, timestamp=timestamp
    )

    finalize_mkp_info(mkp_info, cmk_addon_files_tar_name, cmk_addon_files_list)

    if arg_config.show_info:
        print(json.dumps(mkp_info, indent=4))

    outfile = arg_config.outfile
    if not outfile:
        outfile_name = "{}-{}.mkp".format(mkp_info["name"], mkp_info["version"])
        outfile = os.path.abspath(outfile_name)

    with open(outfile, "wb") as out_fh:
        write_mkp_tar(
            out_fh,
            mkp_info,
            cmk_addon_files_tar_name,
            cmk_addon_files_tar,
            timestamp=timestamp,
        )


if HAVE_YAML:

    def load_yaml_file(filepath: str) -> Any:
        with open(filepath, "rt") as fh:
            file_data = fh.read()

        if file_data:
            return yaml.safe_load(file_data)
        else:
            return None

else:

    def load_yaml_file(filepath: str) -> Any:
        raise RuntimeError(f"yaml module not available, cannot load file {filepath}")


def load_json_file(filepath: str) -> Any:
    with open(filepath, "rt") as fh:
        data = json.load(fh)

    return data


def prepare_mkp_info(
    arg_config: argparse.Namespace, plugin_source: FilesTree
) -> dict[str, Any]:

    def create_default_mkp_info() -> dict[str, Any]:
        return {
            key: default_value
            for key, (is_required, default_value) in Globals.mkp_info_template.items()
        }

    def load_from_mkp_info_file(
        mkp_info: dict[str, Any], plugin_source: FilesTree
    ) -> Any:
        # load metadata from info file
        for fname, fn_load_file in [
            ("info.yml", load_yaml_file),
            ("info.json", load_json_file),
        ]:
            try:
                mkp_info_file = plugin_source.files["info.yml"]

            except KeyError:
                pass

            else:
                info_file_data = fn_load_file(mkp_info_file.path)
                if info_file_data:
                    mkp_info.update(info_file_data)

                return  # BREAK-LOOP on first file found

    # --- end of load_from_mkp_info_file (...) ---

    def add_cmdline_info(
        mkp_info: dict[str, Any], arg_config: argparse.Namespace
    ) -> None:
        # TODO // NOT IMPLEMENTED
        pass

    # --- end of add_cmdline_info (...) ---

    def add_missing_info_from_plugin_source(
        mkp_info: dict[str, Any], plugin_source: FilesTree
    ) -> None:
        def read_var_from_file(
            mkp_info: dict[str, Any],
            plugin_source: FilesTree,
            varname: str,
            filenames: list[str],
        ) -> None:
            for fname in filenames:
                try:
                    finfo = plugin_source.files[fname]

                except KeyError:
                    pass

                else:
                    with open(finfo.path, "rt") as fh:
                        data = fh.read()

                    mkp_info[varname] = data.rstrip()

                    return  # BREAK-LOOP on first file found

        # --- end of read_var_from_file (...) ---

        name = mkp_info["name"]
        if not name:
            name = plugin_source.info.name
            mkp_info["name"] = name

        if not mkp_info["title"]:
            mkp_info["title"] = name

        for varname, filenames in [
            ("author", ["AUTHOR"]),
            ("version", ["VERSION"]),
            ("description", ["README", "README.md"]),
        ]:
            if not mkp_info[varname]:
                read_var_from_file(mkp_info, plugin_source, varname, filenames)

    # --- end of add_missing_info_from_plugin_source (...) ---

    def check_missing_info(mkp_info: dict[str, Any]) -> list[str]:
        missing = []

        for key, (is_required, default_value) in Globals.mkp_info_template.items():
            if is_required:
                try:
                    value = mkp_info[key]
                except KeyError:
                    missing.append(key)

                else:
                    if (value is None) or (
                        isinstance(default_value, str) and not value
                    ):
                        missing.append(key)

        return missing

    # --- end of check_missing_info (...) ---

    mkp_info = create_default_mkp_info()

    load_from_mkp_info_file(mkp_info, plugin_source)
    add_cmdline_info(mkp_info, arg_config)
    add_missing_info_from_plugin_source(mkp_info, plugin_source)

    if missing := check_missing_info(mkp_info):
        raise ValueError(
            "missing mkp info variables: {}".format(", ".join(sorted(missing)))
        )

    # additional checks
    if mkp_info.get("files"):
        raise ValueError("files must not be set in mkp info manually")

    return mkp_info


def finalize_mkp_info(
    mkp_info: dict[str, Any], cmk_addon_files_name: str, cmk_addon_files_list: list[str]
) -> None:
    mkp_info["files"] = {cmk_addon_files_name: cmk_addon_files_list}


def build_cmk_addon_files_tar(
    plugin_source: FilesTree, plugin_name: str, *, timestamp: Optional[float] = None
) -> tuple[list, bytes]:
    def walk_plugin_source(plugin_source: FilesTree) -> Iterator[tuple[bool, FileInfo]]:
        subdir_names_should_exec = {"libexec"}

        yield (False, plugin_source.info)

        # include files from all subdirectories (but not from the top-level dir)
        for subdir in plugin_source.directories.values():
            subdir_name = subdir.info.name

            should_exec = subdir_name in subdir_names_should_exec

            for finfo in subdir:
                yield (should_exec, finfo)

    def get_plugin_tar_relpath(
        finfo: FileInfo, *, osp_join=os.path.join, plugin_name=plugin_name
    ) -> str:
        relpath = finfo.relpath
        if relpath:
            return osp_join(plugin_name, relpath)
        else:
            return plugin_name

    REGTYPE = tarfile.REGTYPE  # ref
    DIRTYPE = tarfile.DIRTYPE  # ref
    TarInfo = tarfile.TarInfo  # ref
    stat_s_isreg = stat.S_ISREG  # ref
    stat_s_isdir = stat.S_ISDIR  # ref

    tar_dirmode = (
        stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )
    tar_filemode_text = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
    tar_filemode_exec = tar_filemode_text | (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    member_filenames = []

    if timestamp is None:
        timestamp = time.time()

    with io.BytesIO() as tar_buffer:
        with tarfile.TarFile.open(
            name=None,
            mode="w|",
            fileobj=tar_buffer,
        ) as tar_fh:
            for should_exec, finfo in walk_plugin_source(plugin_source):
                sb = finfo.stat_info
                fmode = sb.st_mode

                tinfo = TarInfo()

                member_filename = get_plugin_tar_relpath(finfo)

                tinfo.name = member_filename
                tinfo.linkname = ""

                tinfo.mtime = timestamp

                # mkp special: override owner
                tinfo.uid = 0
                tinfo.gid = 0
                tinfo.uname = "root"
                tinfo.gname = "root"

                if stat_s_isreg(fmode):
                    tinfo.size = sb.st_size
                    tinfo.type = REGTYPE
                    tinfo.mode = tar_filemode_exec if should_exec else tar_filemode_text

                    with open(finfo.path, "rb") as member_data_fh:
                        tar_fh.addfile(tinfo, member_data_fh)

                elif stat_s_isdir(fmode):
                    tinfo.size = 0
                    tinfo.type = DIRTYPE
                    tinfo.mode = tar_dirmode

                    tar_fh.addfile(tinfo)

                else:
                    raise ValueError(finfo)

                member_filenames.append(member_filename)

        tar_data = tar_buffer.getvalue()

    return (member_filenames, tar_data)


def write_mkp_tar(
    outfileobj: typing.BinaryIO,
    mkp_info: dict[str, Any],
    cmk_addon_files_tar_name: str,
    cmk_addon_files_tar: bytes,
    timestamp: Optional[float] = None,
) -> None:
    def create_tarinfo_file(
        name: str,
        fsize: int,
        *,
        timestamp: float,
        tar_filemode_text=(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH),
        TarInfo=tarfile.TarInfo,
        REGTYPE=tarfile.REGTYPE,
    ) -> tarfile.TarInfo:
        tinfo = TarInfo()

        tinfo.name = name
        tinfo.linkname = ""

        tinfo.mtime = timestamp

        # mkp special: override owner
        tinfo.uid = 0
        tinfo.gid = 0
        tinfo.uname = "root"
        tinfo.gname = "root"

        tinfo.size = fsize
        tinfo.type = REGTYPE
        tinfo.mode = tar_filemode_text

        return tinfo

    if timestamp is None:
        timestamp = time.time()

    with tarfile.TarFile.open(
        name=None,
        mode="w|gz",
        fileobj=outfileobj,
    ) as tar_fh:
        for fname, fn_convert, add_newline in [
            ("info", pprint.pformat, True),
            ("info.json", json.dumps, False),
        ]:
            with io.BytesIO() as member_data_fh:
                member_data_fh.write(fn_convert(mkp_info).encode("utf-8"))
                if add_newline:
                    member_data_fh.write(b"\n")

                member_data_fh.seek(0, os.SEEK_END)
                fsize = member_data_fh.tell()
                member_data_fh.seek(0, os.SEEK_SET)

                tinfo = create_tarinfo_file(fname, fsize, timestamp=timestamp)
                tar_fh.addfile(tinfo, member_data_fh)
            # --
        # --

        with io.BytesIO() as member_data_fh:
            member_data_fh.write(cmk_addon_files_tar)

            member_data_fh.seek(0, os.SEEK_END)
            fsize = member_data_fh.tell()
            member_data_fh.seek(0, os.SEEK_SET)

            tinfo = create_tarinfo_file(
                cmk_addon_files_tar_name, fsize, timestamp=timestamp
            )
            tar_fh.addfile(tinfo, member_data_fh)
        # --


def scan_plugin_source_directory(root: str) -> FilesTree:
    re_fname_ignore = re.compile(
        (r"(?:" r"~" r"|[.](?:bak|make_tmp|py[codz]|\$py[.]class)" r")$"), flags=re.I
    )

    def check_ignore_all(name: str) -> bool:
        return name[0] == "."

    def check_ignore_dir(name: str) -> bool:
        return name in {"local", "__pycache__"}

    def check_ignore_file(
        name: str, *, re_fname_ignore_search=re_fname_ignore.search
    ) -> bool:
        return re_fname_ignore_search(name)

    def scan_recursive(
        parent_node: FilesTree,
        *,
        os_scandir=os.scandir,  # ref
        osp_join=os.path.join,  # ref
        stat_s_isreg=stat.S_ISREG,  # ref
        stat_s_isdir=stat.S_ISDIR,  # ref
    ) -> None:
        dinfo = parent_node.info
        dirpath_abs = dinfo.path
        dirpath_rel = dinfo.relpath

        if dirpath_rel:
            get_relpath = lambda name, *, _rp=dirpath_rel: osp_join(_rp, name)
        else:
            get_relpath = lambda name: name

        with os_scandir(dirpath_abs) as it:
            for entry in it:
                fname = entry.name

                if check_ignore_all(fname):
                    continue  # LOOP-CONTINUE: name ignored

                finfo = FileInfo(
                    path=osp_join(dirpath_abs, fname),
                    relpath=get_relpath(fname),
                    name=fname,
                    stat_info=entry.stat(follow_symlinks=False),
                )

                fmode = finfo.stat_info.st_mode

                if stat_s_isdir(fmode):
                    if not check_ignore_dir(fname):
                        node = FilesTree(finfo)
                        scan_recursive(node)

                        if node:
                            # skip (effectively) empty directories
                            parent_node.add_directory_node(node)

                elif stat_s_isreg(fmode):
                    if not check_ignore_file(fname):
                        parent_node.add_file(finfo)

                # else ignored

    root_abs = os.path.abspath(root)

    root_info = FileInfo(
        path=root_abs,
        relpath="",
        name=os.path.basename(root_abs),
        stat_info=os.stat(root_abs, follow_symlinks=False),
    )

    ftree = FilesTree(root_info)

    scan_recursive(ftree)

    return ftree


if __name__ == "__main__":
    try:
        exit_code = main(sys.argv[0], sys.argv[1:])

    except KeyboardInterrupt:
        exit_code = 130

    except BrokenPipeError:
        exit_code = 11
        for fh in [sys.stdout, sys.stderr]:
            try:
                fh.close()
            except IOError:
                pass

    else:
        if (exit_code is True) or (exit_code is None):
            exit_code = getattr(os, "EX_OK", 0)

        elif exit_code is False:
            exit_code = getattr(os, "EX_OK", 0) ^ 1

    sys.exit(exit_code)
