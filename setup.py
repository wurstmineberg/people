#!/usr/bin/env python

import setuptools

setuptools.setup(
    name='people',
    version='0.1',
    description='People.json database interface',
    url='http://github.com/wurstmineberg/people',
    author='Wurstmineberg',
    author_email='mail@wurstmineberg.de',
    license='MIT',
    packages=['people'],
    zip_safe=True,
    install_requires=[
        'docopt',
        'dpath',
        'getpass',
        'iso8601',
        'jsonschema',
        'passlib',
        'psycopg2',
        'slacker'
    ]
)
