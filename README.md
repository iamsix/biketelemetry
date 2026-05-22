Bike telemetry reader and sender. 

This is in 2 parts:

## telemetry.py
Collects the data from a USB ant+ sensor and GPS module (as long as it works with `gpsd`) on portable mini PC (Pi, belbox, etc) and periodically POSTs it to a remote server.
I purposely want to keep this script very simple 'data collection and send' without doing any processing of the data.

## hud
**backend.py** collects the data that's sent by **telemety.py**
It also runs a fastapi server and hosts hud.html that can be used on an OBS overlay.
This one does all the hersine calculation for elevation/distance/etc. It could also calc speed but the GPS module gives us that by default so we might as well use it.
Currently it assumes you have some kind of HR and power meter broadcasting on ANT+ - I may eventually look at bluetooth/etc and other possible sensors like a dedicated cadence sensor that's not part of a power meter etc.