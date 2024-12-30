#!/usr/bin/env python3
"""
Get a list of the latest 'LWN.net Weekly Edition' articles.
"""

import argparse
import datetime
import glob
import json
import locale
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from ratelimit import limits, sleep_and_retry

EPILOG = None
TOOL_NAME = os.path.basename(__file__)
ONE_MINUTE = 60
MAX_CALLS_PER_MINUTE = 1


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
    return argparse.ArgumentTypeError(string + '\n\t'.join([''] + str(exception).split('\n')))


def ConfigFileType(string):
    file = argparse.FileType('r')(string)
    _, ext = os.path.splitext(file.name)
    if ext.lower() == '.json':
        parse = json.load
    elif ext.lower() in [ '.yaml', '.yml' ]:
        parse = yaml.safe_load
    else:
        raise argparse.ArgumentTypeError('%r unsupported file type' % string)
    try:
        return parse(file)
    except Exception as e:
        raise ArgumentTypeAppendExceptionError(f'while reading {string}', e)


@contextmanager
def temporary_locale(temp_locale):
    old_locale = locale.getlocale()
    try:
        locale.setlocale(locale.LC_ALL, temp_locale)
        yield
    finally:
        locale.setlocale(locale.LC_ALL, old_locale)[2]


def to_epub_file_path(args, date):
    return os.path.join(
        args.epub_directory,
        args.epub_file_format.format(weekno=date.strftime('%y%W'))
    )


def get_current_epub_id_maps(args):
    page = args.session.get(args.current_url)
    soup = BeautifulSoup(page.text, 'html.parser')
    result = {}
    with temporary_locale('en_US'):
        date_str = re.match(r'.* +for +(.*) +\[.*]', soup.title.text)[1]
        date = datetime.datetime.strptime(date_str, '%B %d, %Y')
        epub_file_path = to_epub_file_path(args, date)
        result[epub_file_path] = re.match(r'.*/Articles/(\w+)/', page.url)[1]
    return result


def get_archive_epub_id_maps(args):
    page = args.session.get(args.archive_url)
    soup = BeautifulSoup(page.text, 'html.parser')
    result = {}
    with temporary_locale('en_US'):
        for a in soup.find_all('a'):
            if 'LWN.net Weekly Edition' in a.get_text():
                date_str = re.match(r'.* +for +(.*) *', a.get_text())[1]
                date = datetime.datetime.strptime(date_str, '%B %d, %Y')
                epub_file_path = to_epub_file_path(args, date)
                result[epub_file_path] = re.match(r'/Articles/(\w+)/', a.get('href'))[1]
    return result


def get_converted_epubs(args):
    return glob.glob(
        os.path.join(
            args.epub_directory,
            args.epub_file_format.format(weekno='[0-9]'*4)
        )
    )


@sleep_and_retry
@limits(calls=MAX_CALLS_PER_MINUTE, period=ONE_MINUTE)
def convert_issue_to_epub(args, dest, issue):
    """
    Converts an LWN weekly issue to an EPUB file
    """
    add_args = []
    if all((args.username, args.password)):
        add_args += [
            f"--username={args.username}",
            f"--password={args.password}"
        ]
    subprocess.run([
        'flatpak', 'run', '--command=/app/bin/ebook-convert', 'com.calibre_ebook.calibre',
        args.ebook_convert_recipe, dest,
    ] + add_args + [
        f'--recipe-specific-option=issue:{issue}',
    ], check=True)


def main() -> int:
    """Main function"""

    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        '--config',
        nargs='*',
        type=ConfigFileType,
        help="""
            Configuration file (or files) to load options from. The format of a configuration file can either be
            YAML or JSON, and the `.yml`, `.yaml` and `.json` file extensions are supported.
            Options need to be provided using the long option names specified for the command-line,
            with separator (`-`) replaced by underbars (`_`) (e.g., use `{ ebook_convert_recipe: [ ... ] }` for specifying the
            `--ebook-convert-recipe` in a JSON configuration file.
            Note that options also specified on the command-line will override configuration file options."""
    )
    parser.add_argument(
        '--login-url',
        type=UrlType,
        help='Login URL for LWN.net',
        default='https://lwn.net/Login/',
    )
    parser.add_argument(
        '--archive-url',
        type=UrlType,
        help='URL for the LWN.net Weekly Edition Archives page',
        default='https://lwn.net/Archives/',
    )
    parser.add_argument(
        '--current-url',
        type=UrlType,
        help='URL for the LWN.net Current Weekly Edition page',
        default='https://lwn.net/current/',
    )
    parser.add_argument(
        '--epub-directory',
        type=ExistingDirectoryType,
        help='Local directory where all the LWN.net Weekly Edition EPUB files are stored',
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
        help='The Calibre ebook-convert recipe to use to convert a Weekly Edition to EPUB',
        default='LWN.net Weekly Edition',
    )
    args = parser.parse_args()
    if args.config is not None:
        for config in args.config:
            parser.set_defaults(**config)
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
        most_recent_epub_id_maps |= get_current_epub_id_maps(args)
    most_recent_epub_id_maps |= get_archive_epub_id_maps(args)
    converted_epubs = get_converted_epubs(args)
    missing_epub_id_maps = {
        k: v for k, v in most_recent_epub_id_maps.items()
        if k not in converted_epubs
    }
    for dest, issue in missing_epub_id_maps.items():
        print(f"Converting issue '{issue}' and saving to '{dest}'")
        convert_issue_to_epub(args, dest, issue)
    return 0


if __name__ == '__main__':
    sys.exit(main())  # next section explains the use of sys.exit
