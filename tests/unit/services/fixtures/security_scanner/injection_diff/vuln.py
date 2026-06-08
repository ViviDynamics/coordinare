"""Fixture: an obvious command/SQL injection for scanner tests.

This file is intentionally vulnerable. It is NOT imported by production code;
it exists only as a sample source for `scan_diff` fixture-based tests.
"""

import os
import sqlite3
import subprocess


def run_user_command(user_input: str) -> None:
    # CWE-78: OS command injection — untrusted input flows into shell.
    os.system("echo " + user_input)
    subprocess.call(user_input, shell=True)


def lookup_user(conn: sqlite3.Connection, name: str):
    # CWE-89: SQL injection — untrusted input concatenated into query.
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE name = '" + name + "'")
    return cur.fetchall()
