"""
Spirit Trading Platform

Automated cryptocurrency trading system with modular strategy architecture.
"""

__version__ = "2.2.3"
__package_name__ = "spirit-platform"

# Git SHA at build time. Default is "unknown" so a source-tree install
# reads it without error; publish.yml rewrites this to ${{ github.sha }}
# at wheel-build time so `pip install spirit-platform` carries a usable
# value (#781). main.py prefers this over a subprocess `git rev-parse`
# call so pipx installs (no .git in site-packages) get a real SHA.
__git_sha__ = "unknown"
