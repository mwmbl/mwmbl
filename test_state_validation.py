#!/usr/bin/env python3
"""Test script to verify state validation in Result schema."""

import sys
import os

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set up Django before importing models
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mwmbl.settings')
import django
django.setup()

from mwmbl.crawler.batch import Result
from mwmbl.tinysearchengine.indexer import DocumentState

print("Testing Result state validation...\n")

# Test valid state
print("Test 1: Valid state (2 = FROM_USER)")
try:
    r1 = Result(url='http://test.com', title='Test', extract='Extract', state=2)
    print(f'✓ Valid state (2) accepted\n')
except Exception as e:
    print(f'✗ Valid state rejected: {e}\n')

# Test invalid state (1)
print("Test 2: Invalid state (1)")
try:
    r2 = Result(url='http://test.com', title='Test', extract='Extract', state=1)
    print('✗ Invalid state (1) was accepted - VALIDATION FAILED\n')
except ValueError as e:
    print(f'✓ Invalid state (1) rejected: {e}\n')

# Test boolean state (True)
print("Test 3: Boolean state (True)")
try:
    r3 = Result(url='http://test.com', title='Test', extract='Extract', state=True)
    print('✗ Boolean state (True) was accepted - VALIDATION FAILED\n')
except ValueError as e:
    print(f'✓ Boolean state (True) rejected: {e}\n')

# Test boolean state (False)
print("Test 4: Boolean state (False)")
try:
    r4 = Result(url='http://test.com', title='Test', extract='Extract', state=False)
    print('✗ Boolean state (False) was accepted - VALIDATION FAILED\n')
except ValueError as e:
    print(f'✓ Boolean state (False) rejected: {e}\n')

# Test None state
print("Test 5: None state")
try:
    r5 = Result(url='http://test.com', title='Test', extract='Extract', state=None)
    print('✓ None state accepted\n')
except Exception as e:
    print(f'✗ None state rejected: {e}\n')

# Test omitted state
print("Test 6: Omitted state")
try:
    r6 = Result(url='http://test.com', title='Test', extract='Extract')
    print('✓ Omitted state accepted\n')
except Exception as e:
    print(f'✗ Omitted state rejected: {e}\n')

print(f'Valid DocumentState values: {[s.value for s in DocumentState]}')
print(f'Valid DocumentState names: {[s.name for s in DocumentState]}')
