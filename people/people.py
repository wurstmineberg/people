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
  --format=<format>  The people.json format version (2 default, 3 partially supported)

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

from passlib.apps import custom_app_context as pwd_context

from docopt import docopt

__version__ = '0.1'
DEFAULT_CONFIG = {
    "connectionstring": "host=localhost dbname=wurstmineberg",
}

def transaction(func):
    def func_wrapper(self, *args, **kwargs):
        with self.conn:
            with self.conn.cursor() as cur:
                return func(self, *args, cur=cur, **kwargs)
    return func_wrapper

class PeopleDB:
    def __init__(self, connectionstring, verbose=False):
        self.connectionstring = connectionstring
        self.conn = psycopg2.connect(connectionstring)
        psycopg2.extensions.register_adapter(dict, psycopg2.extras.Json)
        self.verbose = verbose

    def disconnect(self):
        self.conn.close()
        self.conn = None

    @transaction
    def obj_dump(self, cur=None, version=2):
        cur.execute("SELECT id, data, version FROM people")
        result = cur.fetchall()
        if result:
            if version <= 2:
                obj = []
            else:
                obj = {}
            for uid, data, v in result:
                converter = PersonConverter(uid, data, v)
                if version <= 2:
                    obj.append(converter.get_version(version))
                else:
                    obj[uid] = converter.get_version(version)
        return obj

    @transaction
    def obj_import(self, data, version=2, pretty=True, cur=None):
        """This will import a dict in the database, dropping all previous data!"""
        # Delete all records
        if self.verbose:
            print('Deleting all records...')
        cur.execute("DELETE FROM people")
        if self.verbose:
            print('Importing data...')
        for obj in data['people']:
            if version <= 2:
                items = obj
                wmbid = obj['id']
            else:
                wmbid = obj
                items = data['people'][obj]
            cur.execute("INSERT INTO people (id, data, version) VALUES (%s, %s, %s)", (wmbid, items, version))
        if self.verbose:
            print('Done!')

    def json_dump(self, version=2, pretty=True):
        arr = self.obj_dump(version=version)
        if version <= 2:
            obj = {'people': arr}
        else:
            obj = {'people': arr, 'version': version}
        if pretty:
            return json.dumps(obj, sort_keys=True, indent=4)
        else:
            return json.dumps(obj)

    def json_import(self, string, version=2, pretty=True):
        """This will import a JSON string in the database, dropping all previous data!"""
        data = json.loads(string)
        return self.obj_import(data)

    @transaction
    def person_show(self, person, cur=None):
        cur.execute("SELECT data FROM people WHERE id = %s", (person,))
        result = cur.fetchone()
        if result:
            return result[0]

    @transaction
    def person_get_key(self, person, key, cur=None):
        cur.execute("SELECT data FROM people WHERE id = %s", (person,))
        result = cur.fetchone()
        if result:
            obj = result[0]
            data = dpath.util.get(obj, key, separator='.')
            return data
        else:
            raise KeyError("Key '{}' or person '{}' does not exist in the database".format(key, person))

    @transaction
    def person_set_key(self, person, key, data, cur=None):
        # Select the row for update, this activates row level locking
        # We don't update in place because this is only available in psql 9.5 :(
        cur.execute("SELECT data FROM people WHERE id = %s FOR UPDATE", (person,))
        result = cur.fetchone()
        if not result:
            raise KeyError("Person '{}' does not exist in the database".format(person))
        obj = result[0]

        # Update the data at key
        dpath.util.set(obj, key, data, separator='.')
        cur.execute("UPDATE people SET data = %s WHERE id=%s", (obj, person))

    @transaction
    def person_add(self, uid, cur=None, version=2):
        cur.execute("INSERT INTO people (id, data, version) VALUES (%s, %s, %s)", (uid, {}, version))

    def people_list(self):
        obj = self.obj_dump()
        return [key for key, person in obj.items()]

    @transaction
    def person_has_password(self, uid, cur=None):
        """This checks if the person has a password entry in the DB"""
        cur.execute("SELECT hash FROM users WHERE id = %s", (uid,))
        result = cur.fetchone()
        if result is not None:
            return True
        return False

    @transaction
    def person_set_password(self, uid, password, cur=None):
        """This sets a new password for the person with the uid specified"""
        result = self.person_show(uid)
        if not result:
            raise KeyError("Person '{}' does not exist in the people table. Use person_add first.".format(person))

        # If we made it this far the person exists
        # Generate a password
        hash = pwd_context.encrypt(password)

        # Store it in the DB. This is an upsert, see http://www.the-art-of-web.com/sql/upsert/ for details
        cur.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
        cur.execute("WITH upsert AS (UPDATE users SET hash = %s WHERE id = %s RETURNING *) INSERT INTO users (id, hash) SELECT %s, %s WHERE NOT EXISTS (SELECT * FROM upsert)", (hash, uid, uid, hash))

    @transaction
    def person_verify_password(self, uid, password, cur=None):
        """This verifies the password"""
        try:
            cur.execute("SELECT hash FROM users WHERE id = %s", (uid,))
            result = cur.fetchone()
            if result:
                hash = result[0]
                return pwd_context.verify(password, hash)
        except KeyError:
            return False
        return False


class PersonConverter:
    def __init__(self, uid, person_obj, version):
        self.uid = uid
        self.person_obj = person_obj
        self.version = version

    def get_version(self, version):
        if self.version == version:
            return self.person_obj
        elif self.version == 2 and version == 3:
            return self._convert_v2_v3()
        else:
            raise NotImplementedError("Converting anything other than v2 to v3 is not implemented. "+
                "Wanted to convert from {} to {}".format(self.version, version))

    def _convert_v2_v3(self):
        """Incomplete v2 to v3 converter"""
        oldp = self.person_obj
        newp = {
            'minecraft': {},
            'statusHistory': [{}]
        }

        log_msg = 'Warning: Convert people.json v2 to v3: ID "{}": '.format(self.uid)

        for key, value in oldp.items():
            if key == 'description':
                newp['description'] = value
            elif key == 'favColor':
                newp['favColor'] = value
            elif key == 'fav_item':
                # favitem is now coded into bases. create an empty base
                newp['base'] = [{"tunnelItem": value}]
            elif key == 'gravatar':
                newp['gravatar'] = value
            elif key == 'minecraft':
                if 'minecraft_previous' in oldp:
                    newp['minecraft']['nicks'] = oldp['minecraft_previous'] + [value]
                else:
                    newp['minecraft']['nicks'] = [value]
            elif key == 'minecraft_previous':
                pass
            elif key == 'minecraftUUID':
                newp['minecraft']['uuid'] = value
            elif key == 'name':
                newp['name'] = value
            elif key == 'options':
                newp['options'] = value
            elif key == 'reddit':
                newp['reddit'] = oldp['reddit']
            elif key == 'status':
                if value in ['former', 'founding', 'invited', 'later']:
                    newp['statusHistory'][0]['status'] = value
                elif value == 'postfreeze':
                    newp['statusHistory'][0]['status'] = 'later'
                elif value == 'vetoed':
                    newp['statusHistory'][0]['status'] = 'former'
                    newp['statusHistory'][0]['reason'] = 'vetoed'
            elif key == 'invitedBy':
                if ('status' in oldp and oldp['status'] in ['founding', 'later', 'postfreeze', 'invited']) or 'status' not in oldp:
                    newp['statusHistory'][0]['by'] = value
                else:
                    print(log_msg + 'InvitedBy given but not able to match status. Please fix manually', file=sys.stderr)
            elif key == 'join_date':
                if ('status' in oldp and oldp['status'] in ['founding', 'later', 'postfreeze', 'invited']) or 'status' not in oldp:
                    newp['statusHistory'][0]['date'] = value
                else:
                    print(log_msg + 'join_date given but not able to match status. Please fix manually', file=sys.stderr)
            elif key == 'twitter':
                newp['twitter'] = {
                    'username': value
                }
            elif key == 'website':
                newp['website'] = value
            elif key == 'wiki':
                newp['wiki'] = value
            elif key in ['irc', 'id', 'nicks']:
                pass
            else:
                # Check if we ignored any keys
                print(log_msg + 'Ignoring unkown entry for key {}'.format(key), file=sys.stderr)

        return newp



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

    format_version = 2
    if '--format' in arguments and arguments['--format']:
        format_version = int(arguments['--format'])

    if arguments['dump']:
        data = db.json_dump(version=format_version)
        if not filename or filename == '-':
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

    elif arguments['list']:
        ppl = db.people_list()
        print(ppl)

    elif arguments['add']:
        ppl = db.person_add()

    db.disconnect()
