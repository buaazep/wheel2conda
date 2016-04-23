import argparse
import base64
from collections import defaultdict
import configparser
import csv
from enum import Enum
import hashlib
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import posixpath
import sys
import tarfile
import tempfile
import zipfile

import win_cli_launchers

class Platform(Enum):
    linux = 1
    osx = 2
    win = 3

script_template = """\
#!{interpreter}
from {module} import {func}
if __name__ == '__main__':
    {func}()
"""

PREFIX = '/opt/anaconda1anaconda2anaconda3'

PYTHON_VERSIONS = ['3.5', '3.4', '2.7']
PLATFORM_PAIRS = [
    (Platform.linux, '64'),
    (Platform.linux, '32'),
    (Platform.osx, '64'),
    (Platform.win, '64'),
    (Platform.win, '32'),
]

class CaseSensitiveContextParser(configparser.ConfigParser):
    optionxfrom = staticmethod(str)

class PackageBuilder:
    def __init__(self, wheel_contents, python_version, platform, bitness):
        self.wheel_contents = wheel_contents
        self.python_version = python_version
        self.platform = platform
        self.bitness = bitness
        self.files = []
        self.has_prefix_files = []
        self.py_record_extra = []
        self.build_no = 0

    def record_file(self, arcname, has_prefix=False):
        self.files.append(arcname)
        if has_prefix:
            self.has_prefix_files.append(arcname)

    def record_file_or_dir(self, arcname, src):
        # We're assuming that it will be either a directory or a regular file
        if src.is_dir():
            d = str(src)
            for dirpath, dirnames, filenames in os.walk(d):
                rel_dirpath = os.path.relpath(dirpath, start=d)
                if os.sep == '\\':
                    rel_dirpath = rel_dirpath.replace('\\', '/')
                for f in filenames:
                    p = posixpath.join(arcname, rel_dirpath, f)
                    self.record_file(posixpath.normpath(p))

        else:
            self.record_file(arcname)

    def site_packages_path(self):
        if self.platform is Platform.win:
            return 'Lib/site-packages/'
        else:
            return 'lib/python{}/site-packages/'.format(self.python_version)

    def scripts_path(self):
        if self.platform is Platform.win:
            return 'Scripts/'
        else:
            return 'bin/'

    def build(self, fileobj):
        with tarfile.open(fileobj=fileobj, mode='w:bz2') as tf:
            self.add_module(tf)
            self.create_scripts(tf)
            self.write_pep376_record(tf)
            self.write_index(tf)
            self.write_has_prefix_list(tf)
            self.write_files_list(tf)


    def add_module(self, tf):
        site_packages = self.site_packages_path()
        for src in self.wheel_contents.unpacked.iterdir():
            if src.name.endswith('.data'):
                self._add_data_dir(tf, src)
                continue

            dst = site_packages + src.name
            if src.name.endswith('.dist-info'):
                # Skip RECORD for now, we'll add it later, with rows for scripts
                def exclude_record(ti):
                    return None if ti.name.endswith('RECORD') else ti
                tf.add(str(src), arcname=dst, filter=exclude_record)
                self.record_file_or_dir(dst, src)
                continue

            # Actual module/package file/directory
            tf.add(str(src), arcname=dst)
            self.record_file_or_dir(dst, src)

    def _add_data_dir(self, tf, src):
        for d in src.iterdir():
            if d.name == 'data':
                for f in d.iterdir():
                    tf.add(str(f), arcname=f.name)
                    self.record_file_or_dir(f.name, f)

            else:
                raise NotImplementedError('%s under data dir' % d.name)

    def _py_record_file(self, relpath, contents):
        h = hashlib.sha256(contents)
        digest = base64.urlsafe_b64encode(h.digest()).decode('ascii').rstrip('=')
        self.py_record_extra.append((relpath, 'sha256='+digest, len(contents)))

    def write_pep376_record(self, tf):
        sio = StringIO()
        installed_record = csv.writer(sio)
        if self.platform is Platform.win:
            prefix_from_site_pkgs = '../..'
        else:
            prefix_from_site_pkgs = '../../..'

        with (self.wheel_contents.find_dist_info() / 'RECORD').open() as f:
            wheel_record = csv.reader(f)
            for row in wheel_record:
                path_parts = row[0].split('/')
                if len(path_parts) > 2 \
                        and path_parts[0].endswith('.data') \
                        and path_parts[1] == 'data':
                    row[0] = posixpath.join(prefix_from_site_pkgs, *path_parts[2:])
                installed_record.writerow(row)

        for row in self.py_record_extra:
            path = posixpath.join(prefix_from_site_pkgs, row[0])
            installed_record.writerow((path,) + row[1:])

        record_path = self.site_packages_path() \
                      + self.wheel_contents.find_dist_info().name + '/RECORD'
        ti = tarfile.TarInfo(record_path)
        contents = sio.getvalue().encode('utf-8')
        ti.size = len(contents)
        tf.addfile(ti, BytesIO(contents))
        # The RECORD file was already recorded for conda's files list when the
        # rest of .dist-info was added.

    def _write_script_unix(self, tf, name, contents):
        path = self.scripts_path() + name
        ti = tarfile.TarInfo(path)
        contents = contents.encode('utf-8')
        ti.size = len(contents)
        tf.addfile(ti, BytesIO(contents))
        self.record_file(ti.name, has_prefix=True)
        self._py_record_file(path, contents)

    def _write_script_windows(self, tf, name, contents):
        self._write_script_unix(tf, name+'-script.py', contents)
        arch = 'x64' if self.bitness == '64' else 'x86'
        src = win_cli_launchers.find_exe(arch)
        dst = self.scripts_path() + name + '.exe'
        tf.add(src, arcname=dst)
        self.record_file(dst)
        with open(src, 'rb') as f:
            self._py_record_file(dst, f.read())

    def create_scripts(self, tf):
        ep_file = self.wheel_contents.find_dist_info() / 'entry_points.txt'
        if not ep_file.is_file():
            return

        cp = CaseSensitiveContextParser()
        cp.read([str(ep_file)])
        for name, ep in cp['console_scripts'].items():
            if ep.count(':') != 1:
                raise ValueError("Bad entry point: %r" % ep)
            mod, func = ep.split(':')
            s = script_template.format(
                module=mod, func=func,
                # This is replaced when the package is installed:
                interpreter=PREFIX+'/bin/python',
            )
            if self.platform == Platform.win:
                self._write_script_windows(tf, name, s)
            else:
                self._write_script_unix(tf, name, s)

    def write_index(self, tf):
        py_version_nodot = self.python_version.replace('.', '')
        # TODO: identify dependencies, license
        ix = {
          "arch": "x86_64" if (self.bitness == '64') else 'x86',
          "build": "py{}_{}".format(py_version_nodot, self.build_no),
          "build_number": self.build_no,
          "depends": [
            "python {}*".format(self.python_version)
          ],
          "license": "UNKNOWN",
          "name": self.wheel_contents.metadata['Name'][0],
          "platform": self.platform.name,
          "subdir": "{}-{}".format(self.platform.name, self.bitness),
          "version": self.wheel_contents.metadata['Version'][0]
        }
        contents = json.dumps(ix, indent=2, sort_keys=True).encode('utf-8')
        ti = tarfile.TarInfo('info/index.json')
        ti.size = len(contents)
        tf.addfile(ti, BytesIO(contents))

    def write_has_prefix_list(self, tf):
        lines = [
            '{prefix} text {path}'.format(prefix=PREFIX, path=path)
            for path in self.has_prefix_files
        ]
        contents = '\n'.join(lines).encode('utf-8')
        ti = tarfile.TarInfo('info/has_prefix')
        ti.size = len(contents)
        tf.addfile(ti, BytesIO(contents))

    def write_files_list(self, tf):
        contents = '\n'.join(self.files).encode('utf-8')
        ti = tarfile.TarInfo('info/files')
        ti.size = len(contents)
        tf.addfile(ti, BytesIO(contents))


class BadWheelError(Exception):
    pass

def _read_metadata(path):
    res = defaultdict(list)
    with path.open() as f:
        for line in f:
            if not line.strip():
                break
            k, v = line.strip().split(':', 1)
            k = k.strip()
            v = v.strip()
            res[k].append(v)

    return dict(res)

class WheelContents:
    def __init__(self, whl_file):
        self.td = tempfile.TemporaryDirectory()
        with zipfile.ZipFile(whl_file) as zf:
            zf.extractall(self.td.name)
        self.unpacked = Path(self.td.name)

        self.metadata = _read_metadata(self.find_dist_info() / 'METADATA')

    def find_dist_info(self):
        for x in self.unpacked.iterdir():
            if x.name.endswith('.dist-info'):
                return x

        raise BadWheelError("Didn't find .dist-info directory")

    def check(self):
        dist_info = None
        data_dir = None

        for x in self.unpacked.iterdir():
            if x.name.endswith('.dist-info'):
                if not x.is_dir():
                    raise BadWheelError(".dist-info not a directory")
                if dist_info is not None:
                    raise BadWheelError("Multiple .dist-info directories")
                dist_info = x

            if x.name.endswith('.data'):
                if not x.is_dir():
                    raise BadWheelError(".data not a directory")
                elif data_dir is not None:
                    raise BadWheelError("Multiple .data directories")
                data_dir = x

        if dist_info is None:
            raise BadWheelError("Didn't find .dist-info directory")

        wheel_metadata = _read_metadata(dist_info / 'WHEEL')
        if wheel_metadata['Wheel-Version'][0] != '1.0':
            raise BadWheelError("wheel2conda only knows about wheel format 1.0")
        if wheel_metadata['Root-Is-Purelib'][0].lower() != 'true':
            raise BadWheelError("Can't currently autoconvert packages with platlib")

        for field in ('Name', 'Version'):
            if field not in self.metadata:
                raise BadWheelError("Missing required metadata field: %s" % field)

    def filter_compatible_pythons(self):
        if 'Requires-Python' in self.metadata:
            rp = self.metadata['Requires-Python'][0]
            if rp.startswith(('3', '>3', '>=3')):
                return [p for p in PYTHON_VERSIONS if not p.startswith('2.')]
            elif rp in ('<3', '<3.0'):
                return [p for p in PYTHON_VERSIONS if p.startswith('2.')]

        wheel_metadata = _read_metadata(self.find_dist_info() / 'WHEEL')
        py_tags = {t.split('-')[0] for t in wheel_metadata['Tag']}
        if py_tags == {'py3'}:
            return [p for p in PYTHON_VERSIONS if not p.startswith('2.')]
        elif py_tags == {'py2'}:
            return [p for p in PYTHON_VERSIONS if p.startswith('2.')]

        return PYTHON_VERSIONS
    

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('wheel_file')
    args = ap.parse_args(argv)

    wc = WheelContents(args.wheel_file)
    wc.check()

    for platform, bitness in PLATFORM_PAIRS:
        d = Path(platform.name + '-' + bitness)
        try:
            d.mkdir()
        except FileExistsError:
            pass

        for py_version in wc.filter_compatible_pythons():
            print('Converting for: {}-{},'.format(platform.name, bitness),
                  'Python', py_version)
            pb = PackageBuilder(wc, py_version, platform, bitness)
            filename = '{name}-{version}-py{xy}_0.tar.bz2'.format(
                name = wc.metadata['Name'][0],
                version = wc.metadata['Version'][0],
                xy = py_version.replace('.', ''),
            )
            with (d / filename).open('wb') as f:
                pb.build(f)
    wc.td.cleanup()

if __name__ == '__main__':
    main()
