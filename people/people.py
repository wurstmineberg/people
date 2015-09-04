#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Script to dump and modify the people database

Usage:
  people [options] dump [<filename>]
  people [options] import <filename>
  people [options] getkey <name> [<key>]
  people [options] setkey <name> <key> <value>
  people [options] delkey <name> <key>
  people [options] list
  people [options] gentoken <name>
  people [options] add <person>
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
import uuid

from docopt import docopt

__version__ = '0.1'
DEFAULT_CONFIG = {
    "connectionstring": "postgresql://localhost/wurstmineberg",
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
        cur.execute("SELECT wmbid, data, version FROM people")
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
            cur.execute("INSERT INTO people (wmbid, data, version) VALUES (%s, %s, %s)", (wmbid, items, version))
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
        cur.execute("SELECT data FROM people WHERE wmbid = %s", (person,))
        result = cur.fetchone()
        if result:
            return result[0]

    @transaction
    def person_get_key(self, person, key, cur=None):
        cur.execute("SELECT data FROM people WHERE wmbid = %s", (person,))
        result = cur.fetchone()
        if result:
            obj = result[0]
            return dpath.util.get(obj, key, separator='.')
        else:
            raise KeyError("Person '{}' does not exist in the database".format(person))

    @transaction
    def person_modify_data(self, person, modification_function, cur=None):
        # Select the row for update, this activates row level locking
        # We don't update in place because this is only available in psql 9.5 :(
        cur.execute("SELECT data FROM people WHERE wmbid = %s FOR UPDATE", (person,))
        result = cur.fetchone()
        if not result:
            raise KeyError("Person '{}' does not exist in the database".format(person))
        obj = result[0]

        # Update the data at key
        obj = modification_function(person, obj)

        # Update in database
        cur.execute("UPDATE people SET data = %s WHERE wmbid=%s", (obj, person))

    @transaction
    def person_set_key(self, person, key, data, cur=None):
        def _set_key(person, obj):
            nonlocal key, data
            dpath.util.new(obj, key, data, separator='.')
            return obj

        return self.person_modify_data(person, _set_key)

    @transaction
    def person_del_key(self, person, key, cur=None):
        def _del_key(person, obj):
            nonlocal key
            dpath.util.delete(obj, key, separator='.')
            return obj

        return self.person_modify_data(person, _del_key)

    @transaction
    def person_add(self, uid, cur=None, version=2):
        cur.execute("INSERT INTO people (wmbid, data, version) VALUES (%s, %s, %s)", (uid, {}, version))

    def people_list(self):
        obj = self.obj_dump()
        if type(obj) is list:
            return [obj['id'] for obj in obj]
        else:
            return [key for key, person in obj.items()]

    @transaction
    def person_generate_token(self, uid, cur=None):
        """Generates a one-time token for user registration. Invalidates old tokens"""
        if uid in self.people_list():
            cur.execute("DELETE FROM user_tokens WHERE wmbid = %s", (uid,))
            token = str(uuid.uuid4())
            cur.execute("INSERT INTO user_tokens (wmbid, token) VALUES (%s, %s)", (uid, token))
            return token
        else:
            raise KeyError("Unkown person {}".format(uid))

    @transaction
    def clear_tokens(self, cur=None):
        """Clears all one-time tokens from the database"""
        cur.execute("DELETE FROM user_tokens")


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

    elif arguments['getkey']:
        try:
            if arguments['<key>']:
                data = db.person_get_key(arguments['<name>'], arguments['<key>'])
                print(data)
            else:
                data = db.person_show(arguments['<name>'])
        except KeyError as e:
            print("Key not found: '{}'".format(arguments['<key>']), file=sys.stderr)
            exit(1)

    elif arguments['setkey']:
        value = arguments['<value>']
        try:
            data = json.loads(value)
        except ValueError:
            quotes = ['"', "'"]
            if len(value) >= 2 and value[0] in quotes and value[-1] in quotes:
                # quoted string or JSON
                unquoted = value[1:-1]
                try:
                    data = json.loads(unquoted)
                except ValueError:
                    data = unquoted
            else:
                data = value

        db.person_set_key(arguments['<name>'], arguments['<key>'], data)

    elif arguments['delkey']:
        try:
            db.person_del_key(arguments['<name>'], arguments['<key>'])
        except KeyError as e:
            print("Key doesn't exist: '{}'".format(arguments['<key>']))

    elif arguments['list']:
        ppl = db.people_list()
        print(ppl)

    elif arguments['add']:
        ppl = db.person_add()

    elif arguments['gentoken']:
        if not '<name>' in arguments:
            print('token: No Wurstmineberg ID given')
        else:
            uid = arguments['<name>']
            token = db.person_generate_token(uid)
            print("Generated token for '{}': {}".format(uid, token))


    db.disconnect()
