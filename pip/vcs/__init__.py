"""Handles all VCS (version control) support"""
from __future__ import absolute_import

import logging
import os
import shutil

from pip._vendor.six.moves.urllib import parse as urllib_parse

from pip.exceptions import BadCommand
from pip.utils import (display_path, backup_dir, find_command,
                       rmtree, ask_path_exists)


__all__ = ['vcs', 'get_src_requirement']


logger = logging.getLogger(__name__)


class VcsSupport(object):
    _registry = {}
    schemes = ['ssh', 'git', 'hg', 'bzr', 'sftp', 'svn']

    def __init__(self):
        # Register more schemes with urlparse for various version control
        # systems
        urllib_parse.uses_netloc.extend(self.schemes)
        # Python >= 2.7.4, 3.3 doesn't have uses_fragment
        if getattr(urllib_parse, 'uses_fragment', None):
            urllib_parse.uses_fragment.extend(self.schemes)
        super(VcsSupport, self).__init__()

    def __iter__(self):
        return self._registry.__iter__()

    @property
    def backends(self):
        return list(self._registry.values())

    @property
    def dirnames(self):
        return [backend.dirname for backend in self.backends]

    @property
    def all_schemes(self):
        schemes = []
        for backend in self.backends:
            schemes.extend(backend.schemes)
        return schemes

    def register(self, cls):
        if not hasattr(cls, 'name'):
            logger.warning('Cannot register VCS %s', cls.__name__)
            return
        if cls.name not in self._registry:
            self._registry[cls.name] = cls

    def unregister(self, cls=None, name=None):
        if name in self._registry:
            del self._registry[name]
        elif cls in self._registry.values():
            del self._registry[cls.name]
        else:
            logger.warning('Cannot unregister because no class or name given')

    def get_backend_name(self, location):
        """
        Return the name of the version control backend if found at given
        location, e.g. vcs.get_backend_name('/path/to/vcs/checkout')
        """
        for vc_type in self._registry.values():
            path = os.path.join(location, vc_type.dirname)
            if os.path.exists(path):
                return vc_type.name
        return None

    def get_backend(self, name):
        name = name.lower()
        if name in self._registry:
            return self._registry[name]

    def get_backend_from_location(self, location):
        vc_type = self.get_backend_name(location)
        if vc_type:
            return self.get_backend(vc_type)
        return None


vcs = VcsSupport()


class VersionControl(object):
    name = ''
    dirname = ''

    def __init__(self, url=None, *args, **kwargs):
        self.url = url
        self._cmd = None
        super(VersionControl, self).__init__(*args, **kwargs)

    def _filter(self, line):
        return (logging.DEBUG, line)

    def _is_local_repository(self, repo):
        """
           posix absolute paths start with os.path.sep,
           win32 ones ones start with drive (like c:\\folder)
        """
        drive, tail = os.path.splitdrive(repo)
        return repo.startswith(os.path.sep) or drive

    @property
    def cmd(self):
        if self._cmd is not None:
            return self._cmd
        command = find_command(self.name)
        logger.debug('Found command %r at %r', self.name, command)
        self._cmd = command
        return command

    def get_url_rev(self):
        """
        Returns the correct repository URL and revision by parsing the given
        repository URL
        """
        error_message = (
            "Sorry, '%s' is a malformed VCS url. "
            "The format is <vcs>+<protocol>://<url>, "
            "e.g. svn+http://myrepo/svn/MyApp#egg=MyApp"
        )
        assert '+' in self.url, error_message % self.url
        url = self.url.split('+', 1)[1]
        scheme, netloc, path, query, frag = urllib_parse.urlsplit(url)
        rev = None
        if '@' in path:
            path, rev = path.rsplit('@', 1)
        url = urllib_parse.urlunsplit((scheme, netloc, path, query, ''))
        return url, rev

    def get_info(self, location):
        """
        Returns (url, revision), where both are strings
        """
        assert not location.rstrip('/').endswith(self.dirname), \
            'Bad directory: %s' % location
        return self.get_url(location), self.get_revision(location)

    def normalize_url(self, url):
        """
        Normalize a URL for comparison by unquoting it and removing any
        trailing slash.
        """
        return urllib_parse.unquote(url).rstrip('/')

    def compare_urls(self, url1, url2):
        """
        Compare two repo URLs for identity, ignoring incidental differences.
        """
        return (self.normalize_url(url1) == self.normalize_url(url2))

    def obtain(self, dest):
        """
        Called when installing or updating an editable package, takes the
        source path of the checkout.
        """
        raise NotImplementedError

    def switch(self, dest, url, rev_options):
        """
        Switch the repo at ``dest`` to point to ``URL``.
        """
        raise NotImplementedError

    def update(self, dest, rev_options):
        """
        Update an already-existing repo to the given ``rev_options``.
        """
        raise NotImplementedError

    def check_version(self, dest, rev_options):
        """
        Return True if the version is identical to what exists and
        doesn't need to be updated.
        """
        raise NotImplementedError

    def check_destination(self, dest, url, rev_options, rev_display):
        """
        Prepare a location to receive a checkout/clone.

        Return True if the location is ready for (and requires) a
        checkout/clone, False otherwise.
        """
        checkout = True
        prompt = False
        if os.path.exists(dest):
            checkout = False
            if os.path.exists(os.path.join(dest, self.dirname)):
                existing_url = self.get_url(dest)
                if self.compare_urls(existing_url, url):
                    logger.debug(
                        '%s in %s exists, and has correct URL (%s)',
                        self.repo_name.title(),
                        display_path(dest),
                        url,
                    )
                    if not self.check_version(dest, rev_options):
                        logger.info(
                            'Updating %s %s%s',
                            display_path(dest),
                            self.repo_name,
                            rev_display,
                        )
                        self.update(dest, rev_options)
                    else:
                        logger.info(
                            'Skipping because already up-to-date.')
                else:
                    logger.warning(
                        '%s %s in %s exists with URL %s',
                        self.name,
                        self.repo_name,
                        display_path(dest),
                        existing_url,
                    )
                    prompt = ('(s)witch, (i)gnore, (w)ipe, (b)ackup ',
                              ('s', 'i', 'w', 'b'))
            else:
                logger.warning(
                    'Directory %s already exists, and is not a %s %s.',
                    dest,
                    self.name,
                    self.repo_name,
                )
                prompt = ('(i)gnore, (w)ipe, (b)ackup ', ('i', 'w', 'b'))
        if prompt:
            logger.warning(
                'The plan is to install the %s repository %s',
                self.name,
                url,
            )
            response = ask_path_exists('What to do?  %s' % prompt[0],
                                       prompt[1])

            if response == 's':
                logger.info(
                    'Switching %s %s to %s%s',
                    self.repo_name,
                    display_path(dest),
                    url,
                    rev_display,
                )
                self.switch(dest, url, rev_options)
            elif response == 'i':
                # do nothing
                pass
            elif response == 'w':
                logger.warning('Deleting %s', display_path(dest))
                rmtree(dest)
                checkout = True
            elif response == 'b':
                dest_dir = backup_dir(dest)
                logger.warning(
                    'Backing up %s to %s', display_path(dest), dest_dir,
                )
                shutil.move(dest, dest_dir)
                checkout = True
        return checkout

    def unpack(self, location):
        if os.path.exists(location):
            rmtree(location)
        self.obtain(location)

    def get_src_requirement(self, dist, location, find_tags=False):
        raise NotImplementedError


def get_src_requirement(dist, location, find_tags):
    version_control = vcs.get_backend_from_location(location)
    if version_control:
        try:
            return version_control().get_src_requirement(dist,
                                                         location,
                                                         find_tags)
        except BadCommand:
            logger.warning(
                'cannot determine version of editable source in %s '
                '(%s command not found in path)',
                location,
                version_control.name,
            )
            return dist.as_requirement()
    logger.warning(
        'cannot determine version of editable source in %s (is not SVN '
        'checkout, Git clone, Mercurial clone or Bazaar branch)',
        location,
    )
    return dist.as_requirement()
