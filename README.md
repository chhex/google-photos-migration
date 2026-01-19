# Google Photos to Apple Photos Migration

## Motivation and Background

I wanted to migrate my [Google Photos](https://photos.google.com) library containing a large amount of photos, videos and albums accumulated over the years to [Apple Photos](https://apps.apple.com/ch/app/fotos/id1584215428) and wanted to preserve the album structure and the metadata in the migration.

The process I established in discussing my requirements with [Claude](https://claude.ai) was the following:

1. Export all my photos with [Google Takeout](https://takeout.google.com/) into ZIP files
2. Extract the photo metadata from Google Takeout and embed it into the photos/videos using [ExifTool](https://exiftool.org)
3. Import them using [osxphotos](https://github.com/RhetTbull/osxphotos), creating the albums

Notes:

Concerning 1: [Google Takeout](https://takeout.google.com/) exports reflect the album structure and store the metadata in separate JSON files, not embedded in the photos/videos themselves.

Concerning 2: [ExifTool](https://exiftool.org) extracts the metadata from the JSON files and embeds it back into the exported media files.

Concerning 3: [osxphotos](https://github.com/RhetTbull/osxphotos) is a powerful command-line tool for interacting with Apple Photos.

This script migrates Google Takeout exports to Apple Photos while preserving:

- Album structure
- Photo metadata (date taken, GPS coordinates, descriptions)
- Video metadata

[Claude](https://claude.ai) generated a ready to run python script with its project setup. I did some  testing and provided some smaller enhancements, specially for some corner cases.

## Prerequisites

### Apple Photos Settings

During the import with [osxphotos](https://github.com/RhetTbull/osxphotos), Apple Photos needs to be running.

The option for copying items into the Apple Photos library must be enabled.

After longer batch runs it may be necessary to restart Apple Photos, otherwise unexpected errors may occur.

### Platform

This script runs only on macOS. It was tested on macOS Tahoe 26.2.

Python 3.11 or newer is required. 

### Command Line Tools (via Homebrew)

```bash
# ExifTool - for reading/writing image metadata
brew install exiftool
```

That's it! ExifTool is the only external dependency.

### Python Setup

Requires Python 3.11 or newer (check with `python3 --version`).

```bash
# Navigate to project directory
# Create virtual environment
python3 -m venv .venv

# Activate it
source .venv/bin/activate

# Install dependencies
pip install -e .
```

### Verify Installation

```bash
# Check exiftool
exiftool -ver
# Should output something like: 12.87

# Check osxphotos
osxphotos version
# Should output version info

# Check Python modules
python3 -c "import osxphotos; print('osxphotos OK')"
```

## Configuration

Copy `config.toml.example` to `config.toml` and edit the paths:

```bash
cp config.toml.example config.toml
```

Then edit `config.toml`:

```toml
[paths]
# Where your Takeout ZIPs are
takeout_zip_dir = "/Users/yourname/GoogleTakeout"

# Working directory for extracted files (needs ~1x space of ZIPs)
work_dir = "/Users/yourname/TakeoutProcessing"

# Processed photos ready for import (needs ~1x space of ZIPs)
output_dir = "/Users/yourname/TakeoutReady"
```

## Usage

### Quick Start

```bash
# Activate virtual environment
source .venv/bin/activate

# Run migration
python -m google_photos_migration.migrate
```

### Step by Step

The script offers interactive phase selection:

- `1` = Only extract ZIPs
- `2` = Only process metadata (ZIPs must be extracted already)
- `3` = Only import to Apple Photos (photos must be processed already)
- `A` = All phases (complete migration)
- `T` = Test mode (dry run, no changes)

### Recommended Test Workflow

1. Copy 2-3 ZIPs to a test folder
2. Update `TAKEOUT_ZIP_DIR` to point to test folder
3. Run with `T` (test mode) first
4. Run with `A` if test looks good
5. Verify in Apple Photos
6. If satisfied, process remaining ZIPs

## Project Structure

```
google-photos-migration/
├── config.toml.example     # Configuration template (copy to config.toml)
├── LICENSE                 # MIT License
├── pyproject.toml          # Project configuration
├── README.md               # This file
└── src/
    └── google_photos_migration/
        ├── __init__.py
        └── migrate.py      # Main migration script
```

## Disk Space Requirements

You'll need approximately:

- 1x size of all ZIPs for extraction
- 1x size of all photos for processed output
- Total: ~2x your Google Photos library size

## Troubleshooting

### "osxphotos not found"

```bash
# Make sure virtual environment is activated
source .venv/bin/activate
```

### "exiftool not found"

```bash
brew install exiftool
```

### JSON files not being matched

Google Takeout sometimes truncates filenames or uses inconsistent naming.
Check the `find_json_for_media()` function if you see many unmatched files.

### Album not found after import

Some special Google albums like "Photos from [Year]" may have different naming.
Use `ALBUM_FILTER` to test specific albums first.

## Notes

- The script does NOT modify your original Takeout ZIPs
- Apple Photos must be running during import (osxphotos will start it)
- Large imports (10k+ photos) may take several hours
- Consider running overnight for 310 ZIPs

