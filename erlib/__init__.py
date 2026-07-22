"""Shared library for the Event Recommender pipeline.

Single source of truth for configuration, constants, DB access, the Notion
client, normalisation, date handling, dedup and Google OAuth. Scripts import
from here instead of carrying private copies: scripts/check_no_duplication.py
fails CI when a banned duplicated pattern reappears outside this package.

Introduced by the July 2026 refactor that collapsed six independent Notion
clients (two of which silently dropped every row past 100) into one.
"""
