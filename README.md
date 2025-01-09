# aqualogic_mqtt
MQTT adapter for pool controllers

**THIS IS ALPHA SOFTWARE AND WILL CHANGE!**

This is a Python module that connects to the RS485 interface on certain Hayward Aqualogic pool controllers
and interfaces it with an MQTT broker according to the [automatic discovery convention](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) defined by
[Home Assistant](https://www.home-assistant.io). This means that state information is readable _and_ that control of pumps and relays is 
possible via MQTT (and potentially by Home Assistant).

Connection can be directly via a serial port (recommended) or a serial-to-TCP network adapter.

This software uses the [`aqualogic` module by swilson](https://github.com/swilson/aqualogic) (with some monkey-patching). It requires Python 3.x, paho-mqtt and the original aqualogic Python module. It has been tested with:

* Hayward Aqua Plus (salt water system)
  * Pool only, no spa
  * No heater, no additional wired or wireless remotes
* aqualogic 3.4 (swilson's Python module)
* Python 3.10
* Mosquitto 2.0
* Home Assistant 2024.12

>**NOTE:** Generally speaking, the major difficulty with developing a piece of software like this is that basically no one has more than one pool controller to test with—the author has only one, the system described
above. Bug reports and code contributions are welcome! But, understand that it will be very difficult to support other hardware configurations
at a level beyond guesswork!

This software is not affiliated with or endorsed by Hayward or any other entity in any way. Any trademarks
or other intellectual property remain the property of their respective owners.

## Limitations/TODOs

There are many!

* Not currently possible to set a username or password for MQTT!
* Currently only the Filter, Aux 1, Aux 2, and Super Chlorinate controls are exposed
* Currently only Air/Pool/Spa Temperature, Pool/Spa Chlorinator (%), Salt Level, and Check System sensors are exposed (need to add especially system messages!)
* Serial failures may result in hanging—the process may not exit nor recover, and may have to be killed manually
* Metric unit configured systems are not yet supported
* Not yet possible to use a customized Home Assistant MQTT birth message topic or payload
* Only one pool controller is supported per MQTT broker (please describe your setup in an issue if this affects you 🤨)
* Others I'm forgetting
* Others I don't know about

Fixing several of these will require changing the command line interface,
hence the warnings below.

### IMPORTANT:

It should go without saying, but do not rely on this software for any safety-related function or for freeze protection. _You should leave any safety interlocks and the freeze protection function in the controller enabled._ Only use this software for convenience automation and control—it is not reliable enough for anything else, and never will be. You have been warned!

## Pre-Running

You will probably want to use a [virtual environment](https://docs.python.org/3/library/venv.html) (venv) to install the dependencies, something like this:

```console
$ python3 -m venv ./venv-pool
$ . ./venv-pool/bin/activate
(venv-pool)$ pip install -r requirements.txt
```

This venv should remain activated when you run the module as described below.

## Running

**THE COMMAND LINE INTERFACE WILL CHANGE SOON!**

Currently the module can be started like so:

```
python -m aqualogic_mqtt.client \
  [serial port path] \
  [MQTT hostname]:[MQTT port] \
  [MQTT Discovery Prefix]
```

E.g. 

```console
(venv-pool)$ python -m aqualogic_mqtt.client /dev/ttyUSB0 localhost:1883 homeassistant
```

The MQTT Discovery Prefix determines the "path" on the MQTT broker where
the interface is exposed. For Home assistant, the default is
`homeassistant` unless you have changed it in your configuration.

> **NOTE:** While the topic cannot be covered in depth here, be aware that using multiple USB serial devices (including for example a mix of a USB RS485 interface and Z-Wave or Zigbee stick) may result in unpredictable paths for the serial devices--you may need to set up udev rules to make the correct devices show up at the configured path(s).

It is also possible to connect to a Serial/TCP converter (e.g. a USR-N510) with a host:port, like so
```console
(venv-pool)$ python -m aqualogic_mqtt.client 192.168.1.88:8899 localhost:1883 homeassistant
```
Note, however, that using a network converter such as this has
been found to be unreliable for device control (reading values
usually works well enough).

## Running in a container

**COMING SOON!**

## Using with Home Assistant

Generally, if you already have Home Assistant's MQTT integration set up
and point this software to the same MQTT broker that Home Assistant is
using, then it will "Just Work," with Home Assistant picking up the
entities published into MQTT. If this doesn't happen, check the MQTT
configuration in your Home Assistant instance and make sure that
discovery is enabled and that the discovery prefix matches what you are
providing to this module.

> **NOTE:** It's not yet possible to customize the birth message location and the birth message is used—so this must be set to `[prefix]/status` for now!

## Design Goals

This software is designed with the idea of running on a small SBC (e.g. Raspberry Pi Zero W) connected directly to the pool controller via a serial interface and RS485 adapter, and connected via WiFi to an MQTT broker. This should allow reliable control of the pool system wirelessly. It is likely possible to power the SBC via the 10V output from the controller, such as with [this RS485 HAT](https://www.amazon.com/gp/product/B0BKKXB9JJ/), though this combination has not yet been tested.

Ideally, it should be possible to get this package (or a version of it) 
to build with Micro Python and use an ESP32 or similar device instead
of a heavier-weight SBC--though I expect this will require refactoring
swilson's original module substantially. The architecture of this module
tries to keep this possibility in mind for the future.
