# SPDX-License-Identifier: BSD-3-Clause

import json
from os           import getenv
from pathlib      import Path
from shutil       import copy, make_archive, rmtree

import nox
from nox.sessions import Session

ROOT_DIR  = Path(__file__).parent

BUILD_DIR = ROOT_DIR  / 'build'
CNTRB_DIR = ROOT_DIR  / 'contrib'
DOCS_DIR  = ROOT_DIR  / 'docs'
DIST_DIR  = BUILD_DIR / 'dist'

IN_CI           = getenv('GITHUB_WORKSPACE') is not None
ENABLE_COVERAGE = IN_CI or (getenv('KOKOROWATARI_TEST_COVERAGE') is not None)
LOCAL_TORII_DIR = getenv('LOCAL_TORII_DIR')

# Default sessions to run
nox.options.sessions = (
	'test',
	'lint',
	'typecheck-mypy',
)

# Try to use `uv`, if not fallback to `virtualenv`
nox.options.default_venv_backend = 'uv|virtualenv'

@nox.session(reuse_venv = True)
def test(session: Session) -> None:
	OUTPUT_DIR = BUILD_DIR / 'tests'
	OUTPUT_DIR.mkdir(parents = True, exist_ok = True)
	env: dict[str, str] = dict()

	def _setup_test_env() -> None:
		# Bail out if we are in CI
		if IN_CI:
			return

		TEST_CONFIG = OUTPUT_DIR / 'test_config.json'
		if not TEST_CONFIG.exists():
			return

		with TEST_CONFIG.open('r') as f:
			cfg: dict[str, dict[str, str]] = json.load(f)

		if (debug_cfg := cfg.get('serial')) is not None:
			if (port := debug_cfg.get('port')) is not None:
				env['KOKOROWATARI_SERIAL_TEST_PORT'] = port

			if (baud := debug_cfg.get('baud')) is not None:
				env['KOKOROWATARI_SERIAL_TEST_BAUD'] = baud

	unittest_args = ('-m', 'unittest', 'discover', '-s', str(ROOT_DIR))

	_setup_test_env()

	# XXX(aki):
	# Because we need some things in upstream Torii that are not released, ensure we are
	# using the upstream Git HEAD, otherwise the local dev install will be used if not in CI
	if IN_CI or LOCAL_TORII_DIR is None:
		if LOCAL_TORII_DIR is None:
			session.warn('Kokorowatari uses some unreleased Torii features and bug fixes, and the')
			session.warn('`LOCAL_TORII_DIR` environment variable was not set, falling back to Git')
		session.install('git+https://github.com/shrine-maiden-heavy-industries/torii-hdl.git')
	else:
		if not Path(LOCAL_TORII_DIR).resolve().exists():
			session.error('Environment variable `LOCAL_TORII_DIR` is set but does not exist!')
		session.install('-e', LOCAL_TORII_DIR)

	session.install('-e', '.[dev]')
	if ENABLE_COVERAGE:
		session.log('Coverage support enabled')
		session.install('coverage')
		coverage_args = ('-m', 'coverage', 'run', '-p', f'--rcfile={ROOT_DIR / "pyproject.toml"}',)
		session.env['COVERAGE_CORE'] = 'sysmon'
	else:
		coverage_args = tuple[str]()

	with session.chdir(OUTPUT_DIR):
		session.log('Running core test suite...')
		session.run('python', *coverage_args, *unittest_args, *session.posargs, env = env)

		if ENABLE_COVERAGE:
			session.log('Combining Coverage data..')
			session.run('python', '-m', 'coverage', 'combine')

			session.log('Generating XML Coverage report...')
			session.run('python', '-m', 'coverage', 'xml', f'--rcfile={ROOT_DIR / "pyproject.toml"}')

@nox.session(name = 'watch-docs', reuse_venv = True)
def watch_docs(session: Session) -> None:
	OUTPUT_DIR = BUILD_DIR / 'docs'

	session.install('-r', str(DOCS_DIR / 'requirements.txt'))
	session.install('sphinx-autobuild')
	session.install('-e', '.[dev]')

	session.run('sphinx-autobuild', str(DOCS_DIR), str(OUTPUT_DIR))

@nox.session(name = 'build-docs', reuse_venv = True)
def build_docs(session: Session) -> None:
	OUTPUT_DIR = BUILD_DIR / 'docs'

	session.install('-r', str(DOCS_DIR / 'requirements.txt'))
	session.install('-e', '.[dev]')

	session.run('sphinx-build', '-b', 'html', str(DOCS_DIR), str(OUTPUT_DIR))

@nox.session(name = 'build-docs-multiversion', reuse_venv = True)
def build_docs_multiversion(session: Session) -> None:
	OUTPUT_DIR = BUILD_DIR / 'mv-docs'

	redirect_index = (CNTRB_DIR / 'docs-redirect.html')

	session.install('-r', str(DOCS_DIR / 'requirements.txt'))
	session.install('-e', '.[dev]')

	# Workaround for sphinx-contrib/multiversion#58
	# Ask git for the list of tags matching `v*`, and sort them in reverse order by name
	git_tags: str = session.run(
		'git', 'tag', '-l', 'v*', '--sort=-v:refname',
		external = True, silent = True
	) # type: ignore
	# Split the tags and get the first, it *should* be the most recent
	tags = git_tags.splitlines()
	if len(tags) > 0:
		latest_tag = tags.pop(0)
		latest = ('-D', f'smv_latest_version={latest_tag}',)
	else:
		latest_tag = '_INVALID_'
		latest = tuple[str, ...]()

	# Build the multi-version docs
	session.run(
		'sphinx-multiversion', *latest, str(DOCS_DIR), str(OUTPUT_DIR)
	)

	session.log('Copying docs redirect...')
	# Copy the docs redirect index
	copy(redirect_index, OUTPUT_DIR / 'index.html')

	with session.chdir(OUTPUT_DIR):
		latest_link = Path('latest')
		docs_dev    = Path('main')
		docs_tag    = Path(latest_tag)

		session.log('Copying needed GitHub pages files...')

		copy(docs_dev / 'CNAME', 'CNAME')
		copy(docs_dev / '.nojekyll', '.nojekyll')

		session.log('Creating symlink to latest docs...')
		# If the symlink exists, unlink it
		if latest_link.exists():
			latest_link.unlink()

		# Check to make sure the latest tag has some docs
		if docs_tag.exists():
			# Create a symlink from `/latest` to the latest tag
			latest_link.symlink_to(docs_tag)
		else:
			session.warn(f'Docs for {latest_tag} did not seem to be built, using development docs instead')
			# Otherwise, link to `main`
			latest_link.symlink_to(docs_dev)

@nox.session(name = 'build-docset', reuse_venv = True)
def build_docset(session: Session) -> None:
	DOCS_DIR = BUILD_DIR / 'docs'

	# XXX(aki): We can't `session.notify` here because we need the docs first
	build_docs(session)

	session.install('doc2dash')

	# Get the kokorowatari version
	kokorowatari_version: str = session.run(
		'python', '-c', 'import kokorowatari;print(kokorowatari.__version__)',
		silent = True
	) # type: ignore

	with session.chdir(BUILD_DIR):
		# If the docset is already built, shred it because `doc2dash` won't overwrite it
		if (BUILD_DIR / 'Kokorowatari.docset').exists():
			rmtree(BUILD_DIR / 'Kokorowatari.docset')

		# Build the docset
		session.run(
			'doc2dash', '-n', 'Kokorowatari', '-j', '--full-text-search', 'on', str(DOCS_DIR)
		)

		# Compress it
		make_archive(f'kokorowatari-{kokorowatari_version.strip()}-docset', 'zip', BUILD_DIR, 'Kokorowatari.docset')

@nox.session(name = 'dist-docs', reuse_venv = True)
def dist_docs(session: Session) -> None:
	# XXX(aki): We can't `session.notify` here because we need the docs first
	build_docs(session)

	# Get the kokorowatari version
	kokorowatari_version: str = session.run(
		'python', '-c', 'import kokorowatari;print(kokorowatari.__version__)',
		silent = True
	) # type: ignore

	with session.chdir(BUILD_DIR):
		make_archive(f'kokorowatari-{kokorowatari_version.strip()}-docs', 'zip', BUILD_DIR, 'docs')

@nox.session(name = 'linkcheck-docs', reuse_venv = True)
def linkcheck_docs(session: Session) -> None:
	OUTPUT_DIR = BUILD_DIR / 'docs-linkcheck'

	session.install('-r', str(DOCS_DIR / 'requirements.txt'))
	session.install('-e', '.[dev]')

	session.run('sphinx-build', '-b', 'linkcheck', str(DOCS_DIR), str(OUTPUT_DIR))

@nox.session(name = 'typecheck-mypy', reuse_venv = True)
def typecheck_mypy(session: Session) -> None:
	OUTPUT_DIR = BUILD_DIR / 'typing' / 'mypy'
	OUTPUT_DIR.mkdir(parents = True, exist_ok = True)

	session.install('mypy')
	session.install('lxml')
	session.install('construct-typing')
	session.install('-e', '.[dev]')

	session.run(
		'mypy', '--non-interactive', '--install-types', '--pretty',
		'--disallow-any-generics',
		'--cache-dir', str((OUTPUT_DIR / '.mypy-cache').resolve()),
		'-p', 'kokorowatari', '--html-report', str(OUTPUT_DIR.resolve())
	)

@nox.session(name = 'typecheck-pyright', reuse_venv = True)
def typecheck_pyright(session: Session) -> None:
	OUTPUT_DIR = BUILD_DIR / 'typing' / 'pyright'
	OUTPUT_DIR.mkdir(parents = True, exist_ok = True)

	session.install('pyright')
	session.install('-e', '.[dev]')

	with (OUTPUT_DIR / 'pyright.log').open('w') as f:
		session.run('pyright', *session.posargs, stdout = f)

@nox.session(reuse_venv = True)
def lint(session: Session) -> None:
	session.install('flake8')

	session.run(
		'flake8', '--config', str((CNTRB_DIR / '.flake8').resolve()),
		'./kokorowatari', './tests', './examples', './docs'
	)

@nox.session(reuse_venv = True)
def dist(session: Session) -> None:
	session.install('build')

	session.run('python', '-m', 'build', '-o', str(DIST_DIR))
