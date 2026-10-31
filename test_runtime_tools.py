"""Windows tool discovery must advertise the same entry points execution uses."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autonomous import runtime_tools


@unittest.skipUnless(os.name == 'nt', 'Windows package-manager shim discovery')
class RuntimeToolDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='oss-runtime-tools-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Node With Spaces'
        self.root.mkdir()
        self.paths = {'node': str(self.root / 'node.exe')}
        (self.root / 'node.exe').write_text('fixture; never executed', encoding='utf-8')

    def install(self, name, entry=None):
        self.paths[name] = str(self.root / (name + '.cmd'))
        Path(self.paths[name]).write_text('fixture; never executed', encoding='utf-8')
        if entry:
            file = self.root / 'node_modules' / entry
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('fixture; never executed', encoding='utf-8')
            return str(file)

    def discover(self):
        with patch('autonomous.shutil.which', side_effect=lambda name: self.paths.get(name)):
            return runtime_tools()

    def test_corepack_pnpm_is_available_to_planning(self):
        entry = self.install('pnpm', 'corepack/dist/pnpm.js')
        self.assertEqual([self.paths['node'], entry], self.discover()['pnpm'])

    def test_standalone_pnpm_and_npm_keep_native_node_entry_points(self):
        pnpm = self.install('pnpm', 'pnpm/bin/pnpm.cjs')
        npm = self.install('npm', 'npm/bin/npm-cli.js')
        corepack = self.install('corepack', 'corepack/dist/corepack.js')
        tools = self.discover()
        self.assertEqual([self.paths['node'], pnpm], tools['pnpm'])
        self.assertEqual([self.paths['node'], npm], tools['npm'])
        self.assertEqual([self.paths['node'], corepack], tools['corepack'])

    def test_missing_or_broken_launcher_is_not_advertised(self):
        self.assertIsNone(self.discover()['pnpm'])
        self.install('pnpm')
        self.assertIsNone(self.discover()['pnpm'])

    def test_manager_without_node_is_not_advertised_as_executable(self):
        self.install('pnpm', 'pnpm/bin/pnpm.cjs')
        self.paths.pop('node')
        self.assertIsNone(self.discover()['pnpm'])


if __name__ == '__main__':
    unittest.main()
