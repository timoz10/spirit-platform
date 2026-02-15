"""
utils package

Ensures absolute imports like `from spirit.utils.foo import bar` work regardless of the working directory,
as long as the project root is on sys.path. Most scripts now add the project root dynamically.
"""
