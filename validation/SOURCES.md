# Real footage used for validation (not stored in git; `golden/fetch.sh` and `tests/validate_real.py` rebuild/consume it)
Media is downloaded from Wikimedia Commons, sliced locally and used only to measure the tool. Credit where licences require it.

| name | source file on Commons | licence | slice |
|---|---|---|---|
| panel1, panel2 | President Trump Participates in a Panel Discussion at the Generation Next Summit.webm (The White House) | public domain (US government) | 120-300 s, 420-600 s |
| nasa | Astronaut Jessica Watkins Talks with AccuWeather, KMGH-TV in Colorado.webm (NASA) | public domain | first 180 s |
| bk | Backroom Comics Podcast - Mark Waid Interview at ECCC 2010 Part 2.webm | CC BY 3.0 | 30-180 s |
| ch | Ecology, Ethics, Anarchism - In Conversation with Noam Chomsky - ....webm | CC BY 3.0 | 30-180 s |
| fr (negative control: still photo + podcast audio) | Podcast-Canet-Robert-Saut-episode-3.webm | CC BY-SA 4.0 | 30-180 s |

Labels (who speaks when) were written by reading the Whisper transcripts and checking the frames: see SEGMENTS in tests/validate_real.py.
