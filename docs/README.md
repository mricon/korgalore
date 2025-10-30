# Korgalore Documentation

This directory contains the Sphinx documentation for Korgalore.

## Building the Documentation

Install the requirements:

```bash
pip install -r requirements.txt
```

Build the HTML documentation:

```bash
make html
```

The built documentation will be available in `_build/html/index.html`.

## Documentation Structure

- `index.rst` - Main documentation index
- `installation.rst` - Installation instructions
- `quickstart.rst` - Quick start guide
- `configuration.rst` - Configuration reference
- `usage.rst` - Usage guide and CLI reference
- `contributing.rst` - Contributing guidelines

## ReadTheDocs

This documentation is designed to be built and hosted on ReadTheDocs.

## Local Preview

To preview the documentation locally:

```bash
make html
python -m http.server --directory _build/html
```

Then open http://localhost:8000 in your browser.
