#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Script to dump and modify the people database. Parameters with 'name' always refer to the Wurstmineberg ID for the person.

Usage:
  people [options] dump [<filename>]
  people [options] import <filename>
  people [options] getkey <name> [<key>]
  people [options] setkey <name> <key> <value>
  people [options] delkey <name> <key>
  people [options] list
  people [options] add <name> <status>
  people [options] status <name> <status> [<reason>]
  people (--help | --version)

Options:
  -h, --help        Print this message and exit.
  --config=<config>  Path to the config file [default: /opt/wurstmineberg/config/database.json].
  --version         Print version info and exit.
  -v, --verbose     Print things.
  -f, --force       Don't ask for destructive operations like import
  --format=<format> The people.json format version (3 default, 2 will convert)
  --by=<name>       The user who wants to perform the status change, defaults to shell username if allowed

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
import iso8601

from docopt import docopt

__version__ = '0.1'
DEFAULT_CONFIG = {
    "connectionstring": "postgresql://localhost/wurstmineberg",
}

def transaction(func):
    def func_wrapper(self, *args, **kwargs):
        if 'cur' in kwargs and kwargs['cur'] is not None:
            return func(self, *args, **kwargs)
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
    def obj_dump(self, cur=None, version=3):
        cur.execute("SELECT wmbid, data, version FROM people")
        result = cur.fetchall()
        if result:
            # use v3 as base as the db probably has v3 anyways
            obj = {"version": 3, "people": {}}
            for uid, data, v in result:
                converter = PersonConverter(uid, data, v)
                converter.get_version(3)
                obj['people'][uid] = converter.get_version(3)
            # now for converting everything for realsies
            peopleconv = PeopleConverter(obj)
            return peopleconv.get_version(version)

    @transaction
    def obj_import(self, data, version=3, pretty=True, cur=None):
        """This will import a dict in the database, dropping all previous data!"""
        # Delete all records
        if self.verbose:
            print('Deleting all records...')
        cur.execute("DELETE FROM people")
        if self.verbose:
            print('Importing data...')
        converter = PeopleConverter(data)
        data = converter.get_version(version)
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

    def json_dump(self, version=3, pretty=True):
        obj = self.obj_dump(version=version)
        if pretty:
            return json.dumps(obj, sort_keys=True, indent=4)
        else:
            return json.dumps(obj)

    def json_import(self, string, version=3, pretty=True):
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

    def person_append_status(self, uid, status, by, date, reason=None, cur=None):
        allowed_statuses = ['disabled', 'former', 'founding', 'guest', 'invited', 'later']
        allowed_reasons = ['coc', 'guest', 'inactivity', 'request', 'vetoed']
        if status not in allowed_statuses:
            raise ValueError("Status must be one of {}".format(allowed_statuses))

        if status in ['former', 'disabled']:
            if not reason or reason not in allowed_reasons:
                raise ValueError("Status '{}' must have a reason associated. Value reasons are: {}".format(status, allowed_reasons))
        elif reason:
            raise ValueError("Status '{}' must not have a reason".format(status))

        if not date.tzinfo:
            date = date.replace(tzinfo=datetime.timezone.utc)

        people = self.people_list()
        if by not in people:
            raise ValueError("Status update doesn't have a valid person associated. You must specify a valid wmbid.")

        history = self.person_get_key(uid, 'statusHistory')
        if history[-1]['status'] == status:
            raise ValueError("Status '{}' is the same as the previous status. The status must be different than before.".format(status))

        def _modify(uid, obj):
            nonlocal status, by, date, reason
            status_item = {
                'by': by,
                'status': status,
                'date': date.isoformat()
            }
            if reason:
                status_item['reason'] = reason
            obj['statusHistory'].append(status_item)
            return obj

        return self.person_modify_data(uid, _modify)


    @transaction
    def person_add_empty(self, uid, cur=None, version=3):
        if uid in self.people_list():
            raise ValueError("Person {} already exists. Can't add.".format(uid))
        person = {
            "statusHistory": []
        }
        cur.execute("INSERT INTO people (wmbid, data, version) VALUES (%s, %s, %s)", (uid, person, version))

    @transaction
    def person_delete(self, uid, cur=None):
        cur.execute("DELETE FROM people WHERE wmbid = %s", (uid,))

    @transaction
    def people_list(self, cur=None):
        cur.execute("SELECT wmbid FROM people")
        result = cur.fetchall()
        if result:
            return [p[0] for p in result]

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


class PeopleConverter:
    def __init__(self, obj):
        self.obj = obj
        self.version = 2
        if 'version' in obj:
            self.version = obj['version']

    def get_version(self, version):
        if self.version == version:
            return self.obj
        elif self.version == 2 and version == 3:
            return self._convert_v2_v3()
        elif self.version == 3 and version == 2:
            return self._convert_v3_v2()
        else:
            raise NotImplementedError("Converting anything other than between v2 and v3 is not implemented. "+
                "Wanted to convert from {} to {}".format(self.version, version))

    def _convert_v3_v2(self):
        # We just need to convert the users and sort them.
        v2_people = []
        has_legacy_order = True
        for wmbid, value in self.obj['people'].items():
            personconv = PersonConverter(wmbid, value, 3)
            person = personconv.get_version(2)
            v2_people.append(person)
            if '_peopleV2Order' in value:
                person['_peopleV2Order'] = value['_peopleV2Order']
            else:
                has_legacy_order = False
        if has_legacy_order:
            v2_people.sort(key=lambda p: p['_peopleV2Order'])
        else:
            v2_people.sort(key=lambda p: p['SORT_DATE'])


        # remove the temporary date
        for person in v2_people:
            del person['SORT_DATE']
            if '_peopleV2Order' in person:
                del person['_peopleV2Order']

        return {
            "people": v2_people,
            "version": 2
        }

    def _convert_v2_v3(self):
        # This is even easier.
        people = {}
        index = 0
        for person in self.obj['people']:
            wmbid = person['id']
            # save the order
            personconv = PersonConverter(wmbid, person, 2)
            people[wmbid] = personconv.get_version(3)
            people[wmbid]['_peopleV2Order'] = index
            index += 1

        return {
            "people": people,
            "version": 3
        }



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
        elif self.version == 3 and version == 2:
            return self._convert_v3_v2()
        else:
            raise NotImplementedError("Converting anything other than between v2 and v3 is not implemented. "+
                "Wanted to convert from {} to {}".format(self.version, version))

    def _convert_v2_v3(self):
        """Incomplete v2 to v3 converter"""
        oldp = self.person_obj
        newp = {
            'minecraft': {},
            'statusHistory': []
        }

        log_msg = 'Warning: Convert people.json v2 to v3: ID "{}": '.format(self.uid)
        current_status = {
        }
        previous_status = {
        }

        # 'When not specified, the value is assumed to be "later"'
        if not 'status' in oldp:
            oldp['status'] = 'later'

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
                # we don't care anymore
                pass
            elif key == 'status':
                if value in ['former', 'founding', 'invited', 'later']:
                    current_status['status'] = value
                elif value == 'postfreeze':
                    current_status['status'] = 'later'
                elif value == 'vetoed':
                    current_status['status'] = 'former'
                    current_status['reason'] = 'vetoed'

                if value == 'former':
                    # This is a former member but if they are former they must have been whitelisted before
                    # They can't have been founding though so they must have been 'later'
                    previous_status['status'] = 'later'
                    if 'invitedBy' in oldp:
                        previous_status['by'] = oldp['invitedBy']
            elif key == 'invitedBy':
                if ('status' in oldp and oldp['status'] in ['founding', 'later', 'postfreeze', 'invited']) or 'status' not in oldp:
                    current_status['by'] = value
                else:
                    previous_status['by'] = value
            elif key == 'join_date':
                if ('status' in oldp and oldp['status'] in ['founding', 'later', 'postfreeze', 'invited']) or 'status' not in oldp:
                    current_status['date'] = value
                else:
                    previous_status['date'] = value
            elif key == 'slack':
                newp['slack'] = value
            elif key == 'twitter':
                newp['twitter'] = {
                    'username': value
                }
            elif key == 'website':
                newp['website'] = value
            elif key == 'wiki':
                # best guess
                newp['wiki'] = "User:" + value
            elif key in ['irc', 'id', 'nicks']:
                pass
            else:
                # Print if we ignored any keys
                print(log_msg + 'Ignoring unkown entry for key {}'.format(key), file=sys.stderr)

        if len(previous_status.items()) >= 1:
            newp['statusHistory'].append(previous_status)
        if len(current_status.items()) >= 1:
            newp['statusHistory'].append(current_status)

        return newp

    def _convert_v3_v2(self):
        v3 = self.person_obj
        v2 = {
            'id': self.uid,
            'minecraft_previous': []
        }

        log_msg = 'Warning: Convert people.json v3 to v2: ID "{}": '.format(self.uid)

        for key, value in v3.items():
            if key == 'alt':
                v2['minecraft_previous'].extend(value)
            elif key == 'base':
                # We can't really tell which one is the 'main' base
                # Just return the first base with an item
                for base in value:
                    if 'tunnelItem' in base:
                        v2['fav_item'] = base['tunnelItem']
            elif key == 'description':
                v2['description'] = value
            elif key == 'favColor':
                v2['favColor'] = value
            elif key == 'gravatar':
                v2['gravatar'] = value
            elif key == 'minecraft':
                if 'uuid' in value:
                    v2['minecraftUUID'] = value['uuid']
                if 'nicks' in value:
                    v2['minecraft'] = value['nicks'][0]
                    if len(value['nicks']) >= 2:
                        v2['minecraft_previous'].extend(value['nicks'][1:])
            elif key == 'name':
                v2['name'] = value
            elif key == 'options':
                v2['options'] = value
            elif key == 'statusHistory':
                # Oh well, the fun begins...
                # This was hard going from v2 to v3 but the other way round seems like even more fun.
                # But whatever, let's get this over with.

                # We only care about the first and the last item
                curstatus = value[-1]
                by = curstatus.get('by', None)
                datestr = curstatus.get('date', None)
                reason = curstatus.get('reason', None)
                status = curstatus.get('status', None)

                if status in ['former', 'founding', 'invited', 'later']:
                    # these fields translate pretty much 1:1
                    # we need to care about postfreeze tough. Also vetoes.
                    v2['status'] = status
                    if status == 'later' and datestr is not None:
                        # *sigh*
                        dt = iso8601.parse_date(datestr)
                        if dt > datetime.datetime(2013, 11, 2, 17, 33, 45, tzinfo=datetime.timezone.utc):
                            v2['status'] = 'postfreeze'
                    if status == 'former' and reason == 'vetoed':
                        v2['status'] = 'vetoed'
                elif status == 'disabled':
                    # we can't really do anything else here
                    v2['status'] = 'former'
                elif status == 'guest':
                    # uhm well. help??
                    v2['status'] = 'former'

                # okay now we need to find out where to sort them in the v2 file
                # for this we look for the first date we can find
                # we then save it in a temporary key that is later discarded
                sortdate = None
                for item in value:
                    if 'date' in item:
                        if not sortdate:
                            sortdate = iso8601.parse_date(item['date'])
                        # was there some kind of join activity going on?
                    if 'status' in item and item['status'] in ['former', 'founding', 'later', 'invited', 'guest']:
                        if 'date' in item and not 'join_date' in v2:
                            v2['join_date'] = item['date']
                        if 'by' in item and not 'invitedBy' in v2:
                            v2['invitedBy'] = item['by']
                # We really need a date here. If we couldn't find one just take today
                if not sortdate:
                    print(log_msg + "Doesn't have sort date.", file=sys.stderr)
                    sortdate = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
                v2['SORT_DATE'] = sortdate

            elif key == 'twitter':
                if 'username' in value:
                    v2['twitter'] = value['username']
            elif key == 'website':
                v2['website'] = value
            elif key == 'wiki':
                if value.startswith('User:'):
                    v2['wiki'] = value[len('User:'):]
            elif key in ['mojira', 'openID', 'slack', '_peopleV2Order']:
                pass
            else:
                # Print if we ignored any keys
                print(log_msg + 'Ignoring unkown entry for key {}'.format(key), file=sys.stderr)

        if len(v2['minecraft_previous']) == 0:
            del v2['minecraft_previous']
        return v2



def prompt_yesno(text, default=False):
    sys.stderr.write(text + ' ')
    while True:
        try:
            return strtobool(input().lower())
        except ValueError:
            sys.stdout.write('Please respond with \'y\' or \'n\': ')

if __name__ == "__main__":
    arguments = docopt(__doc__, version='Minecraft people ' + __version__)
    CONFIG = DEFAULT_CONFIG.copy()
    if arguments['--config']:
        CONFIG_FILE = pathlib.Path(arguments['--config'])
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
        wmbid = arguments['<name>']
        status = arguments['<status>']
        by = arguments['--by']

        if status not in ['guest', 'invited']:
            print("Error: You can only add a person with the status guest and invited.", file=sys.stderr)
            exit(1)

        if not by:
            print("Error: Need to specify --by for status guest and invited.", file=sys.stderr)
            exit(1)

        if wmbid in db.people_list():
            print("Error: User '{}' already exists.".format(wmbid), file=sys.stderr)
            exit(1)

        try:
            db.person_add_empty(wmbid)
            date = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
            db.person_append_status(wmbid, status, by, date, reason=None)
        except ValueError as e:
            print("Error: {}".format(e), file=sys.stderr)
            db.person_delete(wmbid)
            exit(1)

    elif arguments['status']:
        wmbid = arguments['<name>']
        status = arguments['<status>']
        reason = arguments.get('<reason>', None)
        by = arguments['--by']

        if not by:
            if status in ['guest', 'invited'] or (status == 'former' and reason == 'vetoed'):
                print("Error: Need to specify --by manually for this status.", file=sys.stderr)
                exit(1)
            else:
                import getpass
                by = getpass.getuser()
                idlist = db.people_list()
                if by not in idlist:
                    print("Unkown user. Please run people.py as your user account to associate this action with you or specify the 'by' parameter.", file=sys.stdout)
                    exit(1)

        date = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)

        try:
            db.person_append_status(wmbid, status, by, date, reason=reason)
        except ValueError as e:
            print("Error: {}".format(e), file=sys.stderr)
            exit(1)

    # currently not used
    #elif arguments['gentoken']:
    #    if not '<name>' in arguments:
    #        print('token: No Wurstmineberg ID given')
    #    else:
    #        uid = arguments['<name>']
    #        token = db.person_generate_token(uid)
    #        print("Generated token for '{}': {}".format(uid, token))


    db.disconnect()
