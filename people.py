#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Script to dump and modify the people database

Usage:
  people [options] dump [<filename>]
  people [options] import <filename>
  people [options] get <name> [<key>]
  people [options] set <name> <key> <value>
  people [options] list
  people -h | --help
  people --version

Options:
  -h, --help         Print this message and exit.
  --config=<config>  Path to the config file [default: /opt/wurstmineberg/config/database.json].
  --version          Print version info and exit.
  --verbose          Print things.
  -f, --force        Don't ask for destructive operations like import

"""

# This script requires python3-psycopg2 and dpath

import datetime
import json
import psycopg2
import psycopg2.extras
import pathlib
import contextlib
import dpath.util
import sys
import os
from distutils.util import strtobool

from docopt import docopt

__version__ = '0.1'
DEFAULT_CONFIG = {
    "connectionstring": "host=localhost dbname=wurstmineberg",
}

class PeopleDB:
    def __init__(self, connectionstring, verbose=False):
        self.connectionstring = connectionstring
        self.conn = psycopg2.connect(connectionstring)
        psycopg2.extensions.register_adapter(dict, psycopg2.extras.Json)
        self.verbose = verbose

    def disconnect(self):
        self.conn.close()
        self.conn = None

    def obj_dump(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT data FROM people")
            result = cur.fetchall()
        if result:
            return [r[0] for r in result]

    def obj_import(self, data, version=2, pretty=True):
        """This will import a dict in the database, dropping all previous data!"""
        if version is not 2:
            raise ValueError("Only people.json version 2 is supported at the moment")
        with self.conn:
            with self.conn.cursor() as cur:
                # Delete all records
                if self.verbose:
                    print('Deleting all records...')
                cur.execute("DELETE FROM people")
                if self.verbose:
                    print('Importing data...')
                for person in data['people']:
                    cur.execute("INSERT INTO people (id, data, version) VALUES (%s, %s, %s)", (person['id'], person, version))
                if self.verbose:
                    print('Done!')

    def json_dump(self, version=2, pretty=True):
        if version is not 2:
            raise ValueError("Only people.json version 2 is supported at the moment")
        arr = self.obj_dump()
        obj = {'people': arr, 'version': version}
        if pretty:
            return json.dumps(obj, sort_keys=True, indent=4)
        else:
            return json.dumps(obj)

    def json_import(self, string, version=2, pretty=True):
        """This will import a JSON string in the database, dropping all previous data!"""
        data = json.loads(string)
        return self.obj_import(data)

    def person_show(self, person):
        with self.conn.cursor() as cur:
            cur.execute("SELECT data FROM people WHERE id = %s", (person,))
            result = cur.fetchone()
        if result:
            return result[0]

    def person_get_key(self, person, key):
        with self.conn.cursor() as cur:
            cur.execute("SELECT data FROM people WHERE id = %s", (person,))
            result = cur.fetchone()
            if result:
                obj = result[0]
                data = dpath.util.get(obj, key, separator='.')
                return data
            else:
                raise KeyError("Key '{}' or person '{}' does not exist in the database".format(key, person))

    def person_set_key(self, person, key, data):
        with self.conn:
            with self.conn.cursor() as cur:
                # Select the row for update, this activates row level locking
                # We don't update in place because this is only available in psql 9.5 :(
                cur.execute("SELECT data FROM people WHERE id = %s FOR UPDATE", (person,))
                result = cur.fetchone()
                if not result:
                    raise KeyError("Person '{}' does not exist in the database".format(person))
                obj = result[0]

                # Update the data at key
                dpath.util.set(obj, key, data, separator='.')
                cur.execute("UPDATE people SET data = %s", (obj,))


def prompt_yesno(text, default=False):
    sys.stderr.write(text + ' ')
    while True:
        try:
            return strtobool(input().lower())
        except ValueError:
            sys.stdout.write('Please respond with \'y\' or \'n\': ')

if __name__ == "__main__":
    arguments = docopt(__doc__, version='Minecraft backup roll ' + __version__)
    CONFIG_FILE = pathlib.Path(arguments['--config'])

    CONFIG = DEFAULT_CONFIG.copy()
    with contextlib.suppress(FileNotFoundError):
        with CONFIG_FILE.open() as config_file:
            CONFIG.update(json.load(config_file))

    verbose = False
    if arguments['--verbose']:
        verbose = True

    force = False
    if arguments['--force']:
        force = True

    db = PeopleDB(CONFIG['connectionstring'], verbose=verbose)

    filename = None
    if '<filename>' in arguments:
        filename = arguments['<filename>']

    if arguments['dump']:
        data = db.json_dump()
        if not filename:
            print(data)
        else:
            if not force and os.path.exists(filename):
                if not prompt_yesno('File exists. Do you want to overwrite the file? All its contents will be lost.'):
                    print('Not overwriting file. Exiting.', file=sys.stderr)
                    exit(1)
            with open(filename, "w") as f:
                f.write(data)
                f.write('\n')

    elif arguments['import']:
        if not filename:
            print('import: No filename given. Specify a filename to import as the last argument.', file=sys.stderr)
            exit(1)
        with open(filename, "r") as f:
            data = f.read()
        if data:
            if not force and not prompt_yesno('Do you REALLY want to clear the database and import the file "{}"?'.format(filename)):
                print('Not importing. Exiting.', file=sys.stderr)
                exit(1)
            db.json_import(data)

    elif arguments['get']:
        try:
            if arguments['<key>']:
                data = db.person_get_key(arguments['<name>'], arguments['<key>'])
                print(data)
            else:
                data = db.person_show(arguments['<name>'])
        except KeyError as e:
            print(e)
            exit(1)

    elif arguments['set']:
        try:
            db.person_set_key(arguments['<name>'], arguments['<key>'], arguments['<value>'])
        except KeyError as e:
            print(e)
            exit(1)

    db.disconnect()
