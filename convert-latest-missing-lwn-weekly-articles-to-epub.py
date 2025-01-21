#!/usr/bin/env python3
"""
Get a list of the latest 'LWN.net Weekly Edition' articles.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse
import feedparser
import time

import requests
import semver
import yaml
from bs4 import BeautifulSoup
from dateutil.parser import parse as parse_date
from ratelimit import limits, sleep_and_retry

EPILOG = None
TOOL_NAME = os.path.basename(__file__)
ONE_SECOND = 1
MAX_CALLS_PER_SECOND = 1
CALIBRE_FLATPAK_APP_ID = 'com.calibre_ebook.calibre'
MIN_SUPPORTED_CALIBRE_VER = '7.23.0'


def UrlType(string):
    """
    URL Type validator for argparse
    """
    url = urlparse(string)
    if all((url.scheme, url.netloc)):
        return string
    raise argparse.ArgumentTypeError(f'not a valid URL: {string}')


def ExistingDirectoryType(string):
    """
    Existing directory Type validator for argparse
    """
    if os.path.isdir(string):
        return string
    raise argparse.ArgumentTypeError(f'not a valid directory: {string}')


def ArgumentTypeAppendExceptionError(string, exception):
    """
    Returns an argument type error that appends the exception details to the
    error message
    """
    return argparse.ArgumentTypeError(
        string + '\n\t'.join(
            [''] + str(exception).split('\n')
        )
    )


def ConfigFileType(string):
    """
    Config file type validator for argparse
    """
    file = argparse.FileType('r')(string)
    _, ext = os.path.splitext(file.name)
    if ext.lower() == '.json':
        parse = json.load
    elif ext.lower() in ['.yaml', '.yml']:
        parse = yaml.safe_load
    else:
        raise argparse.ArgumentTypeError(f'{string!r} unsupported file type')
    try:
        return parse(file)
    except Exception as e:
        raise ArgumentTypeAppendExceptionError(f'while reading {string}', e)


def is_calibre_flatpak_app_installed():
    """
    Checks if the Calibre flatpak app is installed
    """
    if shutil.which('flatpak') is None:
        return False
    try:
        result = subprocess.run(
            [
                'flatpak', 'list', '--app', '--columns=application'
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        if CALIBRE_FLATPAK_APP_ID in result.stdout.strip().split('\n'):
            return True
    except FileNotFoundError:
        pass
    return False


def get_ebook_convert_version(ebook_convert_args):
    """
    Get the installed ebook-convert version
    """
    result = subprocess.run(
        ebook_convert_args + ['--version'],
        capture_output=True,
        text=True,
        check=True,
    )
    m = re.match(r'ebook-convert \(calibre (\d+\.\d+\.\d+)\)', result.stdout)
    if m:
        return m[1]
    raise argparse.ArgumentTypeError(
        f'cannot detect ebook-convert version from {result.stdout!r}'
    )


def EbookConvertAppType(string):
    """
    Ebook convert app type validator for argparse
    """
    if string == 'auto-detect':
        if is_calibre_flatpak_app_installed():
            args = [
                'flatpak', 'run', '--command=/app/bin/ebook-convert',
                CALIBRE_FLATPAK_APP_ID
            ]
        else:
            if not shutil.which('ebook-convert'):
                raise argparse.ArgumentTypeError(
                    f'cannot auto-detect an installation of {string!r}'
                )
            args = ['ebook-convert']
    else:
        if shutil.which(string):
            args = [string]
        else:
            raise argparse.ArgumentTypeError(
                f'{string!r} is missing or not executable'
            )
    version = get_ebook_convert_version(args)
    if semver.compare(version, MIN_SUPPORTED_CALIBRE_VER) < 0:
        raise argparse.ArgumentTypeError(
            f'unsupported ebook-convert version ( {version!r} < '
            f'{MIN_SUPPORTED_CALIBRE_VER!r}): please upgrade calibre'
        )
    return args


def to_epub_file_path(args, date):
    """
    Calculates a converted EPUB file path for a given publication date
    """
    return os.path.join(
        args.epub_directory,
        args.epub_file_format.format(weekno=time.strftime('%y%W', date))
    )


def get_epub_url_maps(args):
    """
    Get a map of hypothetical downloaded EPUB files and their download URLs
    """
    d = feedparser.parse(args.rss_feed_url)
    result = {}
    for e in d.entries:
        epub_file_path = to_epub_file_path(args, e.published_parsed)
        result[epub_file_path] = e.link
    return result


def get_converted_epubs(args):
    """
    Get a list of the converted EPUB files in the EPUB directory
    """
    return glob.glob(
        os.path.join(
            args.epub_directory,
            args.epub_file_format.format(weekno='[0-9]'*4)
        )
    )


@sleep_and_retry
@limits(calls=MAX_CALLS_PER_SECOND, period=ONE_SECOND)
def download_epub(args, dest, url):
    """
    Downloads the URL to the destination, applying rate limiting so that LWN.net
    does not block us
    """
    req = args.session.get(url)
    with open(dest, 'wb') as f:
        f.write(req.content)


def main() -> int:
    """
    Main function
    """
    pre_parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        add_help=False,
    )
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    for p in (pre_parser, parser):
        p.add_argument(
            '--config',
            nargs='*',
            type=ConfigFileType,
            help="""
                Configuration file (or files) to load arguments from.
                Both YAML (.yml, .yaml) and JSON (.json) are supported.
                Arguments are specified using the long option names - i.e.,
                the command-line argument word separators (`-`) need to be
                replaced by underscores (`_`).
                E.g., use `{ "ebook_convert_recipe": ... }` for specifying the
                `--ebook-convert-recipe` in a JSON configuration file.
                Note that each subsequent config file takes precedence over the
                previous one, and command-line arguments take precedence all.
            """,
        )
    parser.add_argument(
        '--login-url',
        type=UrlType,
        help='Login URL for LWN.net',
        default='https://lwn.net/Login/',
    )
    parser.add_argument(
        '--rss-feed-url',
        type=UrlType,
        help='URL for the LWN.net weekly edition RSS feed',
        default='https://lwn.net/headlines/weekly_epub',
    )
    parser.add_argument(
        '--epub-directory',
        type=ExistingDirectoryType,
        help="""
            Local directory where all the LWN.net Weekly Edition EPUB files
            are stored
        """,
        default=os.path.expanduser('~/Books/lwn.net'),
    )
    parser.add_argument(
        '--epub-file-format',
        type=str,
        help='EPUB file format',
        default='lwn.net-{weekno}.epub',
    )
    parser.add_argument(
        '--username',
        type=str,
        help='LWN.net account username',
    )
    parser.add_argument(
        '--password',
        type=str,
        help='LWN.net account username',
    )
    parser.add_argument(
        '--ebook-convert-recipe',
        type=str,
        help="""
            The Calibre ebook-convert recipe to use to convert a Weekly
            Edition to EPUB
        """,
        default='LWN.net Weekly Edition',
    )
    parser.add_argument(
        '--ebook-convert-app',
        type=EbookConvertAppType,
        help="""
            The ebook-convert app to use to convert a Weekly Edition to EPUB.
            Will auto-detect if not specified, preferring the Calibre flatpak
            installation over a local installation.
        """,
        default='auto-detect',
    )
    pre_args, _ = pre_parser.parse_known_args()
    if pre_args.config is not None:
        for arg in pre_args.config:
            parser.set_defaults(**arg)
    args = parser.parse_args()

    most_recent_epub_id_maps = {}
    args.session = requests.Session()
    if all((args.login_url, args.username, args.password)):
        form_data = {
            'uname': args.username,
            'pword': args.password,
        }
        response = args.session.post('https://lwn.net/Login/', data=form_data)
        if response.status_code != 200:
            print("Login failed", file=sys.stderr)
            sys.exit(1)
    most_recent_epub_id_maps = get_epub_url_maps(args)
    converted_epubs = get_converted_epubs(args)
    missing_epub_id_maps = {
        k: v for k, v in most_recent_epub_id_maps.items()
        if k not in converted_epubs
    }
    for dest, url in reversed(missing_epub_id_maps.items()):
        print(f"Downloading '{url}' and saving to '{dest}'")
        download_epub(args, dest, url)
    return 0


if __name__ == '__main__':
    sys.exit(main())  # next section explains the use of sys.exit
