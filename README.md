Bike telemetry reader and sender. 

This is in 2 parts:

## telemetry.py
Collects the data from a USB ant+ sensor and GPS module (as long as it works with `gpsd`) on portable mini PC (Pi, belabox, etc) and periodically POSTs it to a remote server.  
I purposely want to keep this script very simple 'data collection and send' without doing any processing of the data.

Note at the start it also does a ubxtool command to fix a GPS bug on my specific u-blox 7 based USB GPS receiver. You may need to comment that out if you're not using the same one, but if you have the ubxtool anyway it probably won't break anything.

**telemetry_gpx.py** This takes a gpx file and replays it as if it was live data from telemetry.py - mostly used for debug/testing purposes (and slightly outdated since it sends grade)

## hud
**backend.py** collects the data that's sent by **telemety.py**  

It also runs a fastapi server and hosts hud.html that can be used on an OBS overlay.


This one does all the hersine calculation for elevation/distance/etc. It could also calc speed but the GPS module gives us that by default so we might as well use it.


Currently it assumes you have some kind of HR and power meter broadcasting on ANT+ - I may eventually look at bluetooth/etc and other possible sensors like a dedicated cadence sensor that's not part of a power meter etc.