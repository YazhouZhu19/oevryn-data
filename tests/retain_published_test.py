import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/retain-published.sh'
REAL_GIT = shutil.which('git')


def files(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob('*') if path.is_file()}


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='oevryn-retain-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.remote = self.root / 'remote.git'
        self.seed = self.root / 'seed'
        self.worker = self.root / 'worker'
        self.concurrent = self.root / 'concurrent'
        self.runner_temp = self.root / 'runner-temp'
        self.runner_temp.mkdir()
        self.git('init', '--bare', '--initial-branch=main', str(self.remote))
        self.git('init', '--initial-branch=main', str(self.seed))
        self.identity(self.seed)
        (self.seed / 'scripts').mkdir()
        shutil.copyfile(SCRIPT, self.seed / 'scripts/retain-published.sh')
        (self.seed / '.gitignore').write_text('next/\nsource/\n')
        (self.seed / 'code.txt').write_text('original code\n')
        (self.seed / 'published/data').mkdir(parents=True)
        (self.seed / 'published/data/old.json').write_text('{"old":true}\n')
        (self.seed / 'published/deployment.json').write_text('{"runId":"old"}\n')
        self.git('add', '.', cwd=self.seed)
        self.git('commit', '-m', 'Initial publication', cwd=self.seed)
        self.git('remote', 'add', 'origin', str(self.remote), cwd=self.seed)
        self.git('push', 'origin', 'HEAD:main', cwd=self.seed)
        self.git('clone', str(self.remote), str(self.worker))
        self.git('clone', str(self.remote), str(self.concurrent))
        self.identity(self.worker)
        self.identity(self.concurrent)
        self.initial_head = self.remote_head()
        self.initial_published = files(self.worker / 'published')
        self.next = self.worker / 'next'
        (self.next / 'data').mkdir(parents=True)
        (self.next / 'data/new.json').write_text('{"complete":"verified data"}\n')
        (self.next / 'deployment.json').write_text('{"runId":"verified","sourceRevision":"' + 'a' * 40 + '"}\n')
        self.expected_next = files(self.next)
        self.wrapper_directory = self.root / 'bin'
        self.wrapper_directory.mkdir()
        self.calls_path = self.root / 'git-calls.jsonl'
        self.attempts_path = self.root / 'push-attempts'
        self.race_commits_path = self.root / 'race-commits.jsonl'
        wrapper = self.wrapper_directory / 'git'
        wrapper.write_text('''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
actual = os.environ['TEST_REAL_GIT']
with open(os.environ['TEST_GIT_CALLS'], 'a') as out:
    out.write(json.dumps(args) + '\\n')
if args == ['push', 'origin', 'HEAD:main']:
    counter = pathlib.Path(os.environ['TEST_PUSH_ATTEMPTS'])
    attempt = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(attempt))
    if attempt <= int(os.environ.get('TEST_RACES', '0')):
        concurrent = pathlib.Path(os.environ['TEST_CONCURRENT'])
        def git(*values):
            return subprocess.check_output([actual, '-C', str(concurrent), *values], text=True,
                                           stderr=subprocess.STDOUT).strip()
        git('fetch', 'origin', 'main')
        git('reset', '--hard', 'origin/main')
        (concurrent / 'code.txt').write_text('concurrent code change ' + str(attempt) + '\\n')
        git('add', '--', 'code.txt')
        git('commit', '-m', 'Concurrent code commit ' + str(attempt))
        git('push', 'origin', 'HEAD:main')
        with open(os.environ['TEST_RACE_COMMITS'], 'a') as out:
            out.write(json.dumps(git('rev-parse', 'HEAD')) + '\\n')
os.execv(actual, [actual, *args])
''')
        wrapper.chmod(0o755)

    def git(self, *arguments, cwd=None, check=True):
        result = subprocess.run([REAL_GIT, *arguments], cwd=cwd or self.root,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=30)
        if check and result.returncode:
            self.fail(f'git {arguments} failed:\n{result.stdout}')
        return result.stdout.strip() if check else result

    def identity(self, directory):
        self.git('config', 'user.name', 'Retention test', cwd=directory)
        self.git('config', 'user.email', 'retention@example.invalid', cwd=directory)

    def remote_head(self):
        return self.git('--git-dir', str(self.remote), 'rev-parse', 'refs/heads/main')

    def run_retention(self, races=0, overrides=None):
        environment = dict(os.environ)
        environment.update({
            'GITHUB_ACTIONS': 'true', 'GITHUB_WORKSPACE': str(self.worker),
            'RUNNER_TEMP': str(self.runner_temp),
            'PATH': str(self.wrapper_directory) + os.pathsep + os.environ['PATH'],
            'TEST_REAL_GIT': REAL_GIT, 'TEST_GIT_CALLS': str(self.calls_path),
            'TEST_PUSH_ATTEMPTS': str(self.attempts_path),
            'TEST_CONCURRENT': str(self.concurrent), 'TEST_RACES': str(races),
            'TEST_RACE_COMMITS': str(self.race_commits_path),
        })
        environment.update(overrides or {})
        return subprocess.run(['bash', 'scripts/retain-published.sh'], cwd=self.worker,
                              env=environment, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=60)

    def push_attempts(self):
        return int(self.attempts_path.read_text()) if self.attempts_path.exists() else 0

    def assert_no_force_or_rebase(self):
        calls = [json.loads(line) for line in self.calls_path.read_text().splitlines()]
        self.assertFalse(any('rebase' in call for call in calls), calls)
        for call in calls:
            if 'push' in call:
                self.assertEqual(call, ['push', 'origin', 'HEAD:main'])

    def assert_published(self, expected):
        destination = self.root / ('verify-' + str(len(list(self.root.glob('verify-*')))))
        self.git('clone', str(self.remote), str(destination))
        self.assertEqual(files(destination / 'published'), expected)
        return destination

    def assert_only_publication_commit(self):
        changed = self.git('--git-dir', str(self.remote), 'diff-tree', '--no-commit-id',
                           '--name-only', '-r', self.remote_head()).splitlines()
        self.assertTrue(changed)
        self.assertTrue(all(path.startswith('published/') for path in changed), changed)

    def test_normal_retention_only_commits_verified_publication_and_preserves_next(self):
        (self.worker / 'code.txt').write_text('uncommitted build residue\n')
        self.git('add', 'code.txt', cwd=self.worker)
        result = self.run_retention()
        self.assertEqual(result.returncode, 0, result.stdout)
        destination = self.assert_published(self.expected_next)
        self.assertEqual((destination / 'code.txt').read_text(), 'original code\n')
        self.assertEqual(files(self.next), self.expected_next)
        self.assertEqual(self.push_attempts(), 1)
        self.assert_only_publication_commit()
        self.assert_no_force_or_rebase()

    def test_main_advanced_before_retention_is_preserved(self):
        (self.concurrent / 'code.txt').write_text('merged code must survive\n')
        self.git('add', 'code.txt', cwd=self.concurrent)
        self.git('commit', '-m', 'A code PR merged after checkout', cwd=self.concurrent)
        self.git('push', 'origin', 'HEAD:main', cwd=self.concurrent)
        code_head = self.remote_head()
        result = self.run_retention()
        self.assertEqual(result.returncode, 0, result.stdout)
        destination = self.assert_published(self.expected_next)
        self.assertEqual((destination / 'code.txt').read_text(), 'merged code must survive\n')
        self.git('--git-dir', str(self.remote), 'merge-base', '--is-ancestor', code_head, self.remote_head())
        self.assertEqual(files(self.next), self.expected_next)
        self.assert_only_publication_commit()
        self.assert_no_force_or_rebase()

    def test_rejected_first_push_rebuilds_on_concurrent_code_commit(self):
        result = self.run_retention(races=1)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.push_attempts(), 2)
        self.assertIn('[rejected]', result.stdout)
        destination = self.assert_published(self.expected_next)
        self.assertEqual((destination / 'code.txt').read_text(), 'concurrent code change 1\n')
        for commit in map(json.loads, self.race_commits_path.read_text().splitlines()):
            self.git('--git-dir', str(self.remote), 'merge-base', '--is-ancestor', commit, self.remote_head())
        self.assertEqual(files(self.next), self.expected_next)
        self.assert_only_publication_commit()
        self.assert_no_force_or_rebase()

    def test_reset_replaces_script_and_next_but_retains_protected_verified_snapshot(self):
        tracked_next = self.concurrent / 'next'
        tracked_next.mkdir()
        replacement = '{"runId":"new tracked code content"}\n'
        (tracked_next / 'deployment.json').write_text(replacement)
        (self.concurrent / 'scripts/retain-published.sh').write_text(
            '#!/usr/bin/env bash\nexit 99\n')
        self.git('add', '-f', '--', 'next/deployment.json', cwd=self.concurrent)
        self.git('add', '--', 'scripts/retain-published.sh', cwd=self.concurrent)
        self.git('commit', '-m', 'Concurrent code replaces script and introduces tracked next',
                 cwd=self.concurrent)
        self.git('push', 'origin', 'HEAD:main', cwd=self.concurrent)
        code_head = self.remote_head()

        result = self.run_retention()
        self.assertEqual(result.returncode, 0, result.stdout)
        destination = self.assert_published(self.expected_next)
        self.assertEqual((self.next / 'deployment.json').read_text(), replacement)
        self.assertEqual((destination / 'scripts/retain-published.sh').read_text(),
                         '#!/usr/bin/env bash\nexit 99\n')
        self.git('--git-dir', str(self.remote), 'merge-base', '--is-ancestor',
                 code_head, self.remote_head())
        self.assert_only_publication_commit()
        self.assert_no_force_or_rebase()

    def test_three_concurrent_rejections_fail_without_overwriting_remote(self):
        result = self.run_retention(races=3)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.push_attempts(), 3)
        destination = self.assert_published(self.initial_published)
        self.assertEqual((destination / 'code.txt').read_text(), 'concurrent code change 3\n')
        race_commits = list(map(json.loads, self.race_commits_path.read_text().splitlines()))
        self.assertEqual(self.remote_head(), race_commits[-1])
        for commit in race_commits:
            self.git('--git-dir', str(self.remote), 'merge-base', '--is-ancestor', commit, self.remote_head())
        self.assertEqual(files(self.next), self.expected_next)
        self.assert_no_force_or_rebase()

    def test_identical_publication_needs_no_commit_or_push(self):
        shutil.rmtree(self.next)
        shutil.copytree(self.worker / 'published', self.next)
        result = self.run_retention()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.remote_head(), self.initial_head)
        self.assertEqual(self.push_attempts(), 0)
        self.assertEqual(files(self.next), self.initial_published)

    def test_non_actions_or_wrong_workspace_is_rejected_before_mutation(self):
        wrong_workspace = self.root / 'other-workspace'
        wrong_workspace.mkdir()
        for overrides in [{'GITHUB_ACTIONS': 'false'}, {'GITHUB_ACTIONS': ''},
                          {'GITHUB_WORKSPACE': str(wrong_workspace)}]:
            with self.subTest(overrides=overrides):
                result = self.run_retention(overrides=overrides)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.remote_head(), self.initial_head)
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.worker), self.initial_head)
                self.assertEqual(files(self.next), self.expected_next)
                self.assertEqual(self.push_attempts(), 0)

    def test_runner_temp_inside_workspace_or_linked_to_it_is_rejected(self):
        inside = self.worker / 'unsafe-temp'
        inside.mkdir()
        alias = self.root / 'temp-alias'
        alias.symlink_to(inside, target_is_directory=True)
        for directory in [inside, alias]:
            with self.subTest(directory=directory):
                result = self.run_retention(overrides={'RUNNER_TEMP': str(directory)})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.remote_head(), self.initial_head)
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.worker), self.initial_head)
                self.assertEqual(files(self.next), self.expected_next)
                self.assertEqual(self.push_attempts(), 0)

    def test_missing_verified_next_metadata_is_rejected(self):
        (self.next / 'deployment.json').unlink()
        result = self.run_retention()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.remote_head(), self.initial_head)
        self.assertEqual(self.push_attempts(), 0)
        self.assertEqual(files(self.worker / 'published'), self.initial_published)


if __name__ == '__main__':
    unittest.main()
