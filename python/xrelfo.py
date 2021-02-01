# FRR ELF xref extractor
#
# Copyright (C) 2020  David Lamparter for NetDEF, Inc.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; either version 2 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; see the file COPYING; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA

import sys
import os
import struct
import re
import traceback
import json
import argparse

from clippy.uidhash import uidhash
from clippy.elf import *
from clippy import frr_top_src
from tiabwarfo import FieldApplicator

try:
    with open(os.path.join(frr_top_src, 'python', 'xrefstructs.json'), 'r') as fd:
        xrefstructs = json.load(fd)
except FileNotFoundError:
    sys.stderr.write('''
The "xrefstructs.json" file (created by running tiabwarfo.py with the pahole
tool available) could not be found.  It should be included with the sources.
''')
    sys.exit(1)

# constants, need to be kept in sync manually...

XREFT_THREADSCHED = 0x100
XREFT_LOGMSG = 0x200
XREFT_DEFUN = 0x300
XREFT_INSTALL_ELEMENT = 0x301

# LOG_*
priovals = {}
prios = ['0', '1', '2', 'E', 'W', 'N', 'I', 'D']


class XrelfoJson(object):
    def dump(self):
        pass

    def check(self, wopt):
        yield from []

    def to_dict(self, refs):
        pass

class Xref(ELFDissectStruct, XrelfoJson):
    struct = 'xref'
    fieldrename = {'type': 'typ'}
    containers = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._container = None
        if self.xrefdata:
            self.xrefdata.ref_from(self, self.typ)

    def container(self):
        if self._container is None:
            if self.typ in self.containers:
                self._container = self.container_of(self.containers[self.typ], 'xref')
        return self._container

    def check(self, *args, **kwargs):
        if self._container:
            yield from self._container.check(*args, **kwargs)


class Xrefdata(ELFDissectStruct):
    struct = 'xrefdata'

    # uid is all zeroes in the data loaded from ELF
    fieldrename = {'uid': '_uid'}

    def ref_from(self, xref, typ):
        self.xref = xref

    @property
    def uid(self):
        if self.hashstr is None:
            return None
        return uidhash(self.xref.file, self.hashstr, self.hashu32_0, self.hashu32_1)

class XrefPtr(ELFDissectStruct):
    fields = [
        ('xref', 'P', Xref),
    ]

class XrefThreadSched(ELFDissectStruct, XrelfoJson):
    struct = 'xref_threadsched'
Xref.containers[XREFT_THREADSCHED] = XrefThreadSched

class XrefLogmsg(ELFDissectStruct, XrelfoJson):
    struct = 'xref_logmsg'

    def _warn_fmt(self, text):
        yield ((self.xref.file, self.xref.line), '%s:%d: %s (in %s())\n' % (self.xref.file, self.xref.line, text, self.xref.func))

    regexes = [
        (re.compile(r'([\n\t]+)'), 'error: log message contains tab or newline'),
    #    (re.compile(r'^(\s+)'),   'warning: log message starts with whitespace'),
        (re.compile(r'^((?:warn(?:ing)?|error)(?:: )?)', re.I), 'warning: log message starts with severity'),
    ]

    def check(self, wopt):
        if wopt.Wlog_format:
            for rex, msg in self.regexes:
                if not rex.search(self.fmtstring):
                    continue

                if sys.stderr.isatty():
                    items = rex.split(self.fmtstring)
                    out = []
                    for i, text in enumerate(items):
                        if (i % 2) == 1:
                            out.append('\033[41;37;1m%s\033[m' % repr(text)[1:-1])
                        else:
                            out.append(repr(text)[1:-1])

                    excerpt = ''.join(out)

                else:
                    excerpt = repr(self.fmtstring)[1:-1]

                yield from self._warn_fmt('%s: "%s"' % (msg, excerpt))

    def dump(self):
        print('%-60s %s%s %-25s [EC %d] %s' % (
            '%s:%d %s()' % (self.xref.file, self.xref.line, self.xref.func),
            prios[self.priority & 7],
            priovals.get(self.priority & 0x30, ' '),
            self.xref.xrefdata.uid, self.ec, self.fmtstring))

    def to_dict(self, xrelfo):
        jsobj = dict([(i, getattr(self.xref, i)) for i in ['file', 'line', 'func']])
        if self.ec != 0:
            jsobj['ec'] = self.ec
        jsobj['fmtstring'] = self.fmtstring
        jsobj['priority'] = self.priority & 7
        jsobj['type'] = 'logmsg'

        if self.priority & 0x10:
            jsobj.setdefault('flags', []).append('errno')
        if self.priority & 0x20:
            jsobj.setdefault('flags', []).append('getaddrinfo')

        xrelfo['refs'].setdefault(self.xref.xrefdata.uid, []).append(jsobj)

Xref.containers[XREFT_LOGMSG] = XrefLogmsg

class CmdElement(ELFDissectStruct, XrelfoJson):
    struct = 'cmd_element'

    cmd_attrs = { 0: None, 1: 'deprecated', 2: 'hidden'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dict_nodes = []

    def to_dict(self, xrelfo):
        cli = xrelfo.setdefault('cli', {})
        jsobj = cli.setdefault(self.name, {})
        jsobj.update({
            'string': self.string,
            'doc': self.doc,
            'attr': self.cmd_attrs.get(self.attr, self.attr),
            'nodes': self.dict_nodes,
        })
        if jsobj['attr'] is None:
            del jsobj['attr']

        jsobj['defun'] = dict([(i, getattr(self.xref, i)) for i in ['file', 'line', 'func']])

Xref.containers[XREFT_DEFUN] = CmdElement

class XrefInstallElement(ELFDissectStruct, XrelfoJson):
    struct = 'xref_install_element'

    def to_dict(self, xrelfo):
        cli = xrelfo.setdefault('cli', {})
        jsobj = cli.setdefault(self.cmd_element.name, {})
        nodes = jsobj.setdefault('nodes', [])

        nodes.append({
            'node': self.node_type,
            'install': dict([(i, getattr(self.xref, i)) for i in ['file', 'line', 'func']]),
        })
    #    node = cli.setdefault(self.node_type, {})
    #    k, jsobj = self.cmd_element.dict_node(node)
    #    jsobj['install'] = dict([(i, getattr(self.xref, i)) for i in ['file', 'line', 'func']])
    #    node[k] = jsobj

Xref.containers[XREFT_INSTALL_ELEMENT] = XrefInstallElement

# shove in field defs
fieldapply = FieldApplicator(xrefstructs)
fieldapply.add(Xref)
fieldapply.add(Xrefdata)
fieldapply.add(XrefLogmsg)
fieldapply.add(XrefThreadSched)
fieldapply.add(CmdElement)
fieldapply.add(XrefInstallElement)
fieldapply()


class Xrelfo(dict):
    def __init__(self):
        super().__init__({
            'refs': {},
        })
        self._xrefs = []

    def load_file(self, filename):
        if filename.endswith('.la'):
            path, name = os.path.split(filename)
            name = name[:-3]
            filename = os.path.join(path, '.libs', name + '.so')

        while True:
            with open(filename, 'rb') as fd:
                hdr = fd.read(4)
                if hdr == b'\x7fELF':
                    self.load_elf(filename)
                    return

                if hdr[:2] == b'#!':
                    path, name = os.path.split(filename)
                    filename = os.path.join(path, '.libs', name)
                    continue

                if hdr[:1] == b'{':
                    fd.seek(0)
                    self.load_json(fd)
                    return

                raise ValueError('cannot determine file type for %s' % (filename))

    def load_elf(self, filename):
        edf = ELFDissectFile(filename)

        note = edf._elffile.find_note('FRRouting', 'XREF')
        if note is not None:
            endian = '>' if edf._elffile.bigendian else '<'
            mem = edf._elffile[note]
            if edf._elffile.elfclass == 64:
                start, end = struct.unpack(endian + 'QQ', mem)
                start += note.start
                end += note.start + 8
            else:
                start, end = struct.unpack(endian + 'II', mem)
                start += note.start
                end += note.start + 4

            ptrs = edf.iter_data(XrefPtr, slice(start, end))

        else:
            xrefarray = edf.get_section('xref_array')
            if xrefarray is None:
                raise ValueError('file has neither xref note nor xref_array section')

            ptrs = xrefarray.iter_data(XrefPtr)

        for ptr in ptrs:
            self._xrefs.append(ptr.xref)

            container = ptr.xref.container()
            if container is None:
                continue
            container.to_dict(self)

        return edf

    def load_json(self, fd):
        data = json.load(fd)
        for uid, items in data['refs'].items():
            myitems = self['refs'].setdefault(uid, [])
            for item in items:
                if item in myitems:
                    continue
                myitems.append(item)

        return data

    def check(self, checks):
        for xref in self._xrefs:
            yield from xref.check(checks)

def main():
    argp = argparse.ArgumentParser(description = 'FRR xref ELF extractor')
    argp.add_argument('-o', dest='output', type=str, help='write JSON output')
    argp.add_argument('--out-by-file',     type=str, help='write by-file JSON output')
    argp.add_argument('-Wlog-format',      action='store_const', const=True)
    argp.add_argument('binaries', metavar='BINARY', nargs='+', type=str, help='files to read (ELF files or libtool objects)')
    args = argp.parse_args()

    errors = 0
    xrelfo = Xrelfo()

    for fn in args.binaries:
        try:
            xrelfo.load_file(fn)
        except:
            errors += 1
            sys.stderr.write('while processing %s:\n' % (fn))
            traceback.print_exc()

    for option in dir(args):
        if option.startswith('W'):
            checks = sorted(xrelfo.check(args))
            sys.stderr.write(''.join([c[-1] for c in checks]))
            break


    refs = xrelfo['refs']

    counts = {}
    for k, v in refs.items():
        strs = set([i['fmtstring'] for i in v])
        if len(strs) != 1:
            print('\033[31;1m%s\033[m' % k)
        counts[k] = len(v)

    out = xrelfo
    outbyfile = {}
    for uid, locs in refs.items():
        for loc in locs:
            filearray = outbyfile.setdefault(loc['file'], [])
            loc = dict(loc)
            del loc['file']
            filearray.append(loc)

    for k in outbyfile.keys():
        outbyfile[k] = sorted(outbyfile[k], key=lambda x: x['line'])

    if errors:
        sys.exit(1)

    if args.output:
        with open(args.output + '.tmp', 'w') as fd:
            json.dump(out, fd, indent=2, sort_keys=True)
        os.rename(args.output + '.tmp', args.output)

    if args.out_by_file:
        with open(args.out_by_file + '.tmp', 'w') as fd:
            json.dump(outbyfile, fd, indent=2, sort_keys=True)
        os.rename(args.out_by_file + '.tmp', args.out_by_file)

if __name__ == '__main__':
    main()
