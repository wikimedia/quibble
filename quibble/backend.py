import logging
import os
import os.path
import pwd
import subprocess
import sys
import tempfile
import threading
import time

import quibble


def getDBClass(engine):
    this_module = sys.modules[__name__]
    for attr in dir(this_module):
        if engine.lower() == attr.lower():
            return getattr(this_module, attr)
    raise Exception('Backend database engine not supported: %s' % engine)


def stderr_relayer(process, log_function):
    thread = threading.Thread(
        target=stderr_to_log,
        args=(process, log_function))
    thread.start()
    return thread


def stderr_to_log(process, log_function):
    while True:
        line = process.stderr.readline()
        if not line:
            break
        log_function(line.rstrip())


class BackendServer(object):

    server = None

    def __init__(self):
        self.log = logging.getLogger('backend.%s' % self.__class__.__name__)

    def __enter__(self):
        self.start()

    def __exit__(self, *args):
        self.stop()

    def stop(self):
        if self.server is not None:
            self.log.info('Terminating %s' % self.__class__.__name__)
            self.server.terminate()
            try:
                self.server.wait(2)
            except subprocess.TimeoutExpired:
                self.server.kill()  # SIGKILL
            finally:
                self.server = None


class MySQL(BackendServer):

    def __init__(self, user='wikiuser', password='secret', dbname='wikidb'):
        super(MySQL, self).__init__()

        self.user = user
        self.password = password
        self.dbname = dbname

        self.tmpdir = tempfile.TemporaryDirectory(prefix='quibble-mysql-')
        self.rootdir = self.tmpdir.name
        self.log.debug('Root dir: %s' % self.rootdir)

        self.errorlog = os.path.join(self.rootdir, 'error.log')
        self.pidfile = os.path.join(self.rootdir, 'mysqld.pid')
        self.socket = os.path.join(self.rootdir, 'socket')

        self._install_db()

    def _install_db(self):
        self.log.info('Initializing MySQL data directory')
        p = subprocess.Popen([
            'mysql_install_db',
            '--datadir=%s' % self.rootdir,
            '--user=%s' % pwd.getpwuid(os.getuid())[0],
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        outs, errs = p.communicate()
        p.wait()
        if p.returncode != 0:
            raise Exception("FAILED (%s): %s" % (p.returncode, outs))

    def _createwikidb(self):
        self.log.info('Creating the wiki database and grant')
        p = subprocess.Popen([
            'mysql',
            '--user=root',
            '--socket=%s' % self.socket,
            ],
            stdin=subprocess.PIPE,
            )
        grant = ("CREATE DATABASE IF NOT EXISTS %s;"
                 "GRANT ALL ON %s.* TO '%s'@'localhost'"
                 "IDENTIFIED BY '%s';\n" % (
                     self.dbname, self.dbname, self.user, self.password))
        p.communicate(input=grant.encode())
        if p.returncode != 0:
            raise

    def start(self):
        self.log.info('Starting MySQL')
        self.server = subprocess.Popen([
            '/usr/sbin/mysqld',  # fixme drop path
            '--skip-networking',
            '--datadir=%s' % self.rootdir,
            '--log-error=%s' % self.errorlog,
            '--pid-file=%s' % self.pidfile,
            '--socket=%s' % self.socket,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        while not os.path.exists(self.socket):
            if self.server.poll() is not None:
                with open(self.errorlog) as errlog:
                    print(errlog.read())
                raise Exception(
                    "MySQL died during startup (%s)" % self.server.returncode)
            self.log.info("Waiting for MySQL socket")
            time.sleep(1)
        self._createwikidb()
        self.log.info('MySQL is ready')

    def __str__(self):
        return self.socket

    def __del__(self):
        self.stop()


class SQLite(object):

    def __init__(self, dbname='wikidb'):
        self.log = logging.getLogger('backend.SQLite')

        self.dbname = dbname

        self.tmpdir = tempfile.TemporaryDirectory(prefix='quibble-sqlite-')
        self.datadir = self.tmpdir.name
        self.log.debug('Data dir: %s' % self.datadir)

    def start(self):
        # Created by MediaWiki
        pass


class ChromeWebDriver(BackendServer):

    def __init__(self, display=None, port=4444, url_base='/wd/hub'):
        super(ChromeWebDriver, self).__init__()

        self.display = display
        self.port = port
        self.url_base = url_base

    def start(self):
        self.log.info('Starting Chromedriver')
        try:
            prev_display = os.environ.get('DISPLAY', None)
            if self.display:
                # We need DISPLAY in the env for chromium_flags()
                os.environ.update({'DISPLAY': self.display})
            env = {'CHROMIUM_FLAGS': quibble.chromium_flags()}

            if self.display is not None:
                # Pass it to chromedriver
                env.update({'DISPLAY': self.display})

            self.server = subprocess.Popen([
                'chromedriver',
                '--port=%s' % self.port,
                '--url-base=%s' % self.url_base,
                ],
                env=env
            )
        finally:
            if prev_display:
                os.environ.update({'DISPLAY': prev_display})
            elif prev_display is None and self.display:
                del(os.environ['DISPLAY'])


class DevWebServer(BackendServer):

    def __init__(self, port=4881, mwdir=None):
        super(DevWebServer, self).__init__()

        self.port = port
        self.mwdir = mwdir

    def start(self):
        self.log.info('Starting MediaWiki built in webserver')

        router = os.path.join(
            self.mwdir, 'maintenance/dev/includes/router.php')

        self.server = subprocess.Popen([
            'php',
            '-S', '127.0.0.1:%s' % self.port,
            router,
            ],
            cwd=self.mwdir,
            universal_newlines=True,
            bufsize=1,  # line buffered
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        stderr_relayer(self.server, self.log.info)

    def __str__(self):
        return 'http://127.0.0.1:%s' % self.port

    def __repr__(self):
        return '<DevWebServer :%s %s>' % (self.port, self.mwdir)

    def __del__(self):
        self.stop()


class Xvfb(BackendServer):

    def __init__(self, display=':94'):
        super(Xvfb, self).__init__()
        self.display = display

    def start(self):
        self.log.info('Starting Xvfb on display %s' % self.display)
        self.server = subprocess.Popen([
            'Xvfb', self.display,
            '-screen', '0', '1280x1024x24'
            '-ac',
            '-nolisten', 'tcp'
            ])
