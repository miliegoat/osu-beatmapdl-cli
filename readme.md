osu tui downloader

simple terminal ui app to search and download osu! beatmaps

features
- osu api v2 search
- filters (mode, status, sort)
- multi-select + batch download
- parallel downloads (faster)
- auto mirror fallback (if one fails)
- progress + speed display
- theme switching

requirements
- python 3.10+
- requests
- textual

setup
1. install deps:
   pip install requests textual

2. get osu api creds:
   go to https://osu.ppy.sh/home/account/edit, go into the oauth section, make a new application, copy the id into the client id field and the secret into the client secret field in the program

3. run:
   python main.py

usage
- enter client id + secret → authenticate
- search beatmaps
- use space / ctrl+a to select
- press d or enter to download
- set download folder if needed

notes
- downloads use multiple mirrors, some may fail
- max 3 downloads at once (change in code if needed)
- files saved as .osz

controls
- q → quit
- space → select
- ctrl+a → select all
- d / enter → download
- ctrl+t → switch theme