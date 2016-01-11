# Copyright 2012-2015 Canonical Ltd. Copyright 2015 Alburnum Ltd.
# This software is licensed under the GNU Affero General Public
# License version 3 (see LICENSE).

"""Commands for interacting with a remote MAAS."""

__all__ = []

from abc import (
    ABCMeta,
    abstractmethod,
)
import argparse
from os import environ
import sys
from textwrap import fill
from time import sleep
from urllib.parse import urlparse

from alburnum.maas import (
    bones,
    utils,
    viscera,
)
from alburnum.maas.bones import CallError
from alburnum.maas.utils.auth import (
    obtain_credentials,
    obtain_password,
    obtain_token,
)
from alburnum.maas.utils.creds import Credentials
import argcomplete
import colorclass

from . import (
    tables,
    tabular,
)


def check_valid_apikey(_1, _2, _3):  # TODO
    return True


def colorized(text):
    if sys.stdout.isatty():
        # Don't return value_colors; returning the Color instance allows
        # terminaltables to correctly calculate alignment and padding.
        return colorclass.Color(text)
    else:
        return colorclass.Color(text).value_no_colors


class ArgumentParser(argparse.ArgumentParser):
    """Specialisation of argparse's parser with better support for subparsers.

    Specifically, the one-shot `add_subparsers` call is disabled, replaced by
    a lazily evaluated `subparsers` property.
    """

    def __init__(self, *args, **kwargs):
        if "formatter_class" not in kwargs:
            kwargs["formatter_class"] = argparse.RawDescriptionHelpFormatter
        super(ArgumentParser, self).__init__(*args, **kwargs)

    def add_subparsers(self):
        raise NotImplementedError(
            "add_subparsers has been disabled")

    @property
    def subparsers(self):
        try:
            return self.__subparsers
        except AttributeError:
            parent = super(ArgumentParser, self)
            self.__subparsers = parent.add_subparsers(title="drill down")
            self.__subparsers.metavar = "COMMAND"
            return self.__subparsers

    def error(self, message):
        """Make the default error messages more helpful

        Override default ArgumentParser error method to print the help menu
        generated by ArgumentParser instead of just printing out a list of
        valid arguments.
        """
        self.exit(2, colorized("{autored}Error:{/autored} ") + message + "\n")


class CommandError(Exception):
    """A command has failed during execution."""


class Command(metaclass=ABCMeta):
    """A base class for composing commands.

    This adheres to the expectations of `register`.
    """

    def __init__(self, parser):
        super(Command, self).__init__()
        self.parser = parser

    @abstractmethod
    def __call__(self, options):
        """Execute this command."""

    @classmethod
    def name(cls):
        """Return the preferred name as which this command will be known."""
        name = cls.__name__.replace("_", "-").lower()
        name = name[4:] if name.startswith("cmd-") else name
        return name

    @classmethod
    def register(cls, parser, name=None):
        """Register this command as a sub-parser of `parser`.

        :type parser: An instance of `ArgumentParser`.
        """
        help_title, help_body = utils.parse_docstring(cls)
        command_parser = parser.subparsers.add_parser(
            cls.name() if name is None else name, help=help_title,
            description=help_title, epilog=help_body)
        command_parser.set_defaults(execute=cls(command_parser))


class TableCommand(Command):

    def __init__(self, parser):
        super(TableCommand, self).__init__(parser)
        if sys.stdout.isatty():
            default_target = tabular.RenderTarget.pretty
        else:
            default_target = tabular.RenderTarget.plain
        parser.add_argument(
            "--output-format", type=tabular.RenderTarget,
            choices=tabular.RenderTarget, default=default_target, help=(
                "Output tabular data as a formatted table (pretty), a "
                "formatted table using only ASCII for borders (plain), or "
                "one of several dump formats. Default: %(default)s."
            ),
        )


class cmd_login_base(Command):

    def __init__(self, parser):
        super(cmd_login_base, self).__init__(parser)
        parser.add_argument(
            "profile_name", metavar="profile-name", help=(
                "The name with which you will later refer to this remote "
                "server and credentials within this tool."
                ))
        parser.add_argument(
            "url", type=utils.api_url, help=(
                "The URL of the remote API, e.g. http://example.com/MAAS/ "
                "or http://example.com/MAAS/api/1.0/ if you wish to specify "
                "the API version."))
        parser.add_argument(
            '-k', '--insecure', action='store_true', help=(
                "Disable SSL certificate check"), default=False)

    def save_profile(self, options, credentials):
        # Check for bogus credentials. Do this early so that the user is not
        # surprised when next invoking the MAAS CLI.
        if credentials is not None:
            try:
                valid_apikey = check_valid_apikey(
                    options.url, credentials, options.insecure)
            except CallError as e:
                raise SystemExit("%s" % e)
            else:
                if not valid_apikey:
                    raise SystemExit("The MAAS server rejected your API key.")

        # Establish a session with the remote API.
        session = bones.SessionAPI.fromURL(
            options.url, credentials=credentials, insecure=options.insecure)

        # Save the config.
        profile_name = options.profile_name
        with utils.ProfileConfig.open() as config:
            config[profile_name] = {
                "credentials": credentials,
                "description": session.description,
                "name": profile_name,
                "url": options.url,
                }
            profile = config[profile_name]

        return profile

    @staticmethod
    def print_whats_next(profile):
        """Explain what to do next."""
        what_next = [
            "{{autogreen}}Congratulations!{{/autogreen}} You are logged in "
            "to the MAAS server at {{autoblue}}{url}{{/autoblue}} with the "
            "profile name {{autoblue}}{name}{{/autoblue}}.",
            "For help with the available commands, try:",
            "  maas --help",
            ]
        for message in what_next:
            message = message.format(**profile)
            print(colorized(fill(message)))
            print()


class cmd_login(cmd_login_base):
    """Log in to a remote MAAS with username and password.

    If credentials are not provided on the command-line, they will be prompted
    for interactively.
    """

    def __init__(self, parser):
        super(cmd_login, self).__init__(parser)
        parser.add_argument(
            "username", nargs="?", default=None, help=(
                "The username used to login to MAAS. Omit this and the "
                "password for anonymous API access."))
        parser.add_argument(
            "password", nargs="?", default=None, help=(
                "The password used to login to MAAS. Omit both the username "
                "and the password for anonymous API access, or pass a single "
                "hyphen to allow the password to be provided via stdin. If a "
                "username is provided but no password, you will be prompted "
                "interactively for it."
            ),
        )

    def __call__(self, options):
        url = urlparse(options.url)

        if options.username is None:
            username = url.username
        else:
            if url.username is None or len(url.username) == 0:
                username = options.username
            else:
                raise CommandError(
                    "Username provided on command-line (%r) and in URL (%r); "
                    "provide only one." % (options.username, url.username))

        if options.password is None:
            password = url.password
        else:
            if url.password is None or len(url.password) == 0:
                password = options.password
            else:
                raise CommandError(
                    "Password provided on command-line (%r) and in URL (%r); "
                    "provide only one." % (options.password, url.password))

        if username is None:
            if password is None or len(password) == 0:
                credentials = None  # Anonymous.
            else:
                raise CommandError(
                    "Password provided without username; specify username.")
        else:
            password = obtain_password(password)
            if password is None:
                raise CommandError("No password supplied.")
            else:
                credentials = obtain_token(
                    options.url, username, password)

        # Save a new profile, and print something useful.
        profile = self.save_profile(options, credentials)
        self.print_whats_next(profile)


class cmd_login_api(cmd_login_base):
    """Log in to a remote MAAS with an *API key*.

    If credentials are not provided on the command-line, they will be prompted
    for interactively.
    """

    def __init__(self, parser):
        super(cmd_login_api, self).__init__(parser)
        parser.add_argument(
            "credentials", nargs="?", default=None, help=(
                "The credentials, also known as the API key, for the "
                "remote MAAS server. These can be found in the user "
                "preferences page in the web UI; they take the form of "
                "a long random-looking string composed of three parts, "
                "separated by colons."
                ))

    def __call__(self, options):
        # Try and obtain credentials interactively if they're not given, or
        # read them from stdin if they're specified as "-".
        credentials = obtain_credentials(options.credentials)
        # Save a new profile, and print something useful.
        profile = self.save_profile(options, credentials)
        self.print_whats_next(profile)


class cmd_logout(Command):
    """Log out of a remote API, purging any stored credentials.

    This will remove the given profile from your command-line  client.  You
    can re-create it by logging in again later.
    """

    def __init__(self, parser):
        super(cmd_logout, self).__init__(parser)
        parser.add_argument(
            "profile_name", metavar="profile-name", help=(
                "The name with which a remote server and its credentials "
                "are referred to within this tool."
                ))

    def __call__(self, options):
        with utils.ProfileConfig.open() as config:
            del config[options.profile_name]


class cmd_list_profiles(TableCommand):
    """List remote APIs that have been logged-in to."""

    def __call__(self, options):
        table = tables.ProfilesTable()
        with utils.ProfileConfig.open() as config:
            print(table.render(options.output_format, config))


class cmd_refresh_profiles(Command):
    """Refresh the API descriptions of all profiles.

    This retrieves the latest version of the help information for each
    profile.  Use it to update your command-line client's information after
    an upgrade to the MAAS server.
    """

    def __call__(self, options):
        with utils.ProfileConfig.open() as config:
            for profile_name in config:
                profile = config[profile_name]
                url, creds = profile["url"], profile["credentials"]
                session = bones.SessionAPI.fromURL(
                    url, credentials=Credentials(*creds))
                profile["description"] = session.description
                config[profile_name] = profile


class OriginCommandBase(Command):

    def __init__(self, parser):
        super(OriginCommandBase, self).__init__(parser)
        default = environ.get("MAAS_PROFILE")
        parser.add_argument(
            "--profile-name", metavar="NAME", required=(default is None),
            default=default, help=(
                "The name of the remote MAAS instance to use. Use "
                "`list-profiles` to obtain a list of valid profiles. "
                "This can also be set via the MAAS_PROFILE environment "
                "variable."
            ))


class OriginCommand(OriginCommandBase):

    def __call__(self, options):
        session = bones.SessionAPI.fromProfileName(options.profile_name)
        origin = viscera.Origin(session)
        return self.execute(origin, options)

    def execute(self, options, origin):
        raise NotImplementedError(
            "Implement execute() in subclasses.")


class OriginTableCommand(OriginCommandBase, TableCommand):

    def __call__(self, options):
        session = bones.SessionAPI.fromProfileName(options.profile_name)
        origin = viscera.Origin(session)
        return self.execute(origin, options, target=options.output_format)

    def execute(self, options, origin, *, target):
        raise NotImplementedError(
            "Implement execute() in subclasses.")


class cmd_list_nodes(OriginTableCommand):
    """List nodes."""

    def execute(self, origin, options, target):
        table = tables.NodesTable()
        print(table.render(target, origin.Nodes))


class cmd_list_tags(OriginTableCommand):
    """List tags."""

    def execute(self, origin, options, target):
        table = tables.TagsTable()
        print(table.render(target, origin.Tags))


class cmd_list_files(OriginTableCommand):
    """List files."""

    def execute(self, origin, options, target):
        table = tables.FilesTable()
        print(table.render(target, origin.Files))


class cmd_list_users(OriginTableCommand):
    """List users."""

    def execute(self, origin, options, target):
        table = tables.UsersTable()
        print(table.render(target, origin.Users))


class cmd_acquire_node(OriginTableCommand):
    """Acquire a node."""

    def __init__(self, parser):
        super(cmd_acquire_node, self).__init__(parser)
        parser.add_argument("--hostname")
        parser.add_argument("--architecture")
        parser.add_argument("--cpus", type=int)
        parser.add_argument("--memory", type=float)
        parser.add_argument("--tags", default="")

    def acquire(self, origin, options):
        return origin.Nodes.acquire(
            hostname=options.hostname, architecture=options.architecture,
            cpus=options.cpus, memory=options.memory,
            tags=options.tags.split())

    def execute(self, origin, options, target):
        node = self.acquire(origin, options)
        table = tables.NodesTable()
        print(table.render(target, [node]))


class cmd_launch_node(cmd_acquire_node):
    """Acquire and deploy a node."""

    def __init__(self, parser):
        super(cmd_launch_node, self).__init__(parser)
        parser.add_argument(
            "--wait", type=int, default=0, help=(
                "Number of seconds to wait for deploy to complete."))

    def execute(self, origin, options, target):
        node = self.acquire(origin, options)
        table = tables.NodesTable()

        print(colorized("{automagenta}DEPLOYING:{/automagenta}"))
        print(table.render(target, [node]))

        with utils.Spinner():
            node = node.start()
            for elapsed, remaining, wait in utils.retries(options.wait, 1.0):
                if node.substatus_name == "Deploying":
                    sleep(wait)
                    node = origin.Node.read(system_id=node.system_id)
                else:
                    break

        if node.substatus_name == "Deployed":
            print(colorized("{autogreen}DEPLOYED:{/autogreen}"))
            print(table.render(target, [node]))
        else:
            print(colorized("{autored}FAILED TO DEPLOY:{/autored}"))
            print(table.render(target, [node]))
            raise CommandError("Node was not deployed.")


class cmd_release_node(OriginTableCommand):
    """Release a node."""

    def __init__(self, parser):
        super(cmd_release_node, self).__init__(parser)
        parser.add_argument("--system-id", required=True)
        parser.add_argument(
            "--wait", type=int, default=0, help=(
                "Number of seconds to wait for release to complete."))

    def execute(self, origin, options, target):
        node = origin.Node.read(system_id=options.system_id)
        node = node.release()

        with utils.Spinner():
            for elapsed, remaining, wait in utils.retries(options.wait, 1.0):
                if node.substatus_name == "Releasing":
                    sleep(wait)
                    node = origin.Node.read(system_id=node.system_id)
                else:
                    break

        table = tables.NodesTable()
        print(table.render(target, [node]))

        if node.substatus_name != "Ready":
            raise CommandError("Node was not released.")


def prepare_parser(argv):
    """Create and populate an argument parser."""
    parser = ArgumentParser(
        description="Interact with a remote MAAS server.", prog=argv[0],
        epilog="http://maas.ubuntu.com/")

    # Basic commands.
    cmd_login.register(parser)
    cmd_login_api.register(parser)
    cmd_logout.register(parser)
    cmd_list_profiles.register(parser)
    cmd_refresh_profiles.register(parser)

    # Other commands.
    cmd_list_nodes.register(parser)
    cmd_list_tags.register(parser)
    cmd_list_files.register(parser)
    cmd_list_users.register(parser)

    # Node lifecycle.
    cmd_acquire_node.register(parser)
    cmd_launch_node.register(parser)
    cmd_release_node.register(parser)

    parser.add_argument(
        '--debug', action='store_true', default=False,
        help=argparse.SUPPRESS)

    return parser


def main(argv=sys.argv):
    parser = prepare_parser(argv)
    argcomplete.autocomplete(parser)

    try:
        options = parser.parse_args(argv[1:])
        try:
            execute = options.execute
        except AttributeError:
            parser.error("No arguments given.")
        else:
            execute(options)
    except KeyboardInterrupt:
        raise SystemExit(1)
    except Exception as error:
        if options.debug:
            raise
        else:
            # Note: this will call sys.exit() when finished.
            parser.error("%s" % error)
