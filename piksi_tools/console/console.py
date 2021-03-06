#!/usr/bin/env python
# Copyright (C) 2011-2014 Swift Navigation Inc.
# Contact: Fergus Noble <fergus@swift-nav.com>
#
# This source is subject to the license found in the file 'LICENSE' which must
# be be distributed together with this source. All other rights reserved.
#
# THIS CODE AND INFORMATION IS PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND,
# EITHER EXPRESSED OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND/OR FITNESS FOR A PARTICULAR PURPOSE.

import os
import piksi_tools.serial_link as s
import sbp.client as sbpc
import signal
import sys

from piksi_tools.serial_link import swriter, get_uuid, DEFAULT_BASE
from piksi_tools.version import VERSION as CONSOLE_VERSION
from sbp.client.drivers.pyftdi_driver import PyFTDIDriver
from sbp.client.drivers.pyserial_driver import PySerialDriver
from sbp.ext_events import *
from sbp.logging import *
from sbp.piksi import SBP_MSG_RESET, MsgReset

# Shut chaco up for now
import warnings
warnings.simplefilter(action = "ignore", category = FutureWarning)

def get_args():
  """
  Get and parse arguments.
  """
  import argparse
  parser = argparse.ArgumentParser(description='Swift Nav Console.')
  parser.add_argument('-p', '--port', nargs=1, default=[None],
                      help='specify the serial port to use.')
  parser.add_argument('-b', '--baud', nargs=1, default=[s.SERIAL_BAUD],
                      help='specify the baud rate to use.')
  parser.add_argument("-v", "--verbose",
                      help="print extra debugging information.",
                      action="store_true")
  parser.add_argument("-l", "--log",
                      action="store_true",
                      help="serialize SBP messages to log file.")
  parser.add_argument("-o", "--log-filename",
                      default=[s.LOG_FILENAME], nargs=1,
                      help="file to log output to. If a directory is provided the "
                           "filename is autogenerated.")
  parser.add_argument("-i", "--initloglevel",
                      default=[None], nargs=1,
                      help="Set log level filter.")
  parser.add_argument("-r", "--reset",
                      action="store_true",
                      help="reset device after connection.")
  parser.add_argument("-u", "--update",
                      help="don't prompt about firmware/console updates.",
                      action="store_false")
  parser.add_argument("-f", "--ftdi",
                      help="use pylibftdi instead of pyserial.",
                      action="store_true")
  parser.add_argument('-t', '--toolkit', nargs=1, default=[None],
                      help="specify the TraitsUI toolkit to use, either 'wx' or 'qt4'.")
  parser.add_argument('-e', '--expert', action='store_true',
                      help="Show expert settings.")
  return parser.parse_args()

args = get_args()
port = args.port[0]
baud = args.baud[0]
log_filename = args.log_filename[0]

# Toolkit
from traits.etsconfig.api import ETSConfig
if args.toolkit[0] is not None:
  ETSConfig.toolkit = args.toolkit[0]
else:
  ETSConfig.toolkit = 'qt4'

# Logging
import logging
logging.basicConfig()
from piksi_tools.console.output_list import OutputList, LogItem, str_to_log_level, \
  SYSLOG_LEVELS, DEFAULT_LOG_LEVEL_FILTER
from piksi_tools.console.utils import determine_path
from piksi_tools.console.deprecated import DeprecatedMessageHandler
from traits.api import Str, Instance, Dict, HasTraits, Int, Button, List, Enum
from traitsui.api import Item, Label, View, HGroup, VGroup, VSplit, HSplit, Tabbed, \
                         InstanceEditor, EnumEditor, ShellEditor, Handler, Spring, \
                         TableEditor, UItem
from traitsui.table_filter \
    import EvalFilterTemplate, MenuFilterTemplate, RuleFilterTemplate, \
           EvalTableFilter
from traitsui.table_column \
    import ObjectColumn, ExpressionColumn


# When bundled with pyInstaller, PythonLexer can't be found. The problem is
# pygments.lexers is doing some crazy magic to load up all of the available
# lexers at runtime which seems to break when frozen.
#
# The horrible workaround is to load the PythonLexer class explicitly and then
# manually insert it into the pygments.lexers module.
from pygments.lexers.agile import PythonLexer
import pygments.lexers
pygments.lexers.PythonLexer = PythonLexer
try:
  import pygments.lexers.c_cpp
except ImportError:
  pass

# These imports seem to be required to make pyinstaller work?
# (usually traitsui would load them automatically)
if ETSConfig.toolkit == 'qt4':
  import pyface.ui.qt4.resource_manager
  import pyface.ui.qt4.python_shell
from pyface.image_resource import ImageResource

basedir = determine_path()
icon = ImageResource('icon', search_path=['images', os.path.join(basedir, 'images')])

from piksi_tools.console.tracking_view import TrackingView
from piksi_tools.console.solution_view import SolutionView
from piksi_tools.console.baseline_view import BaselineView
from piksi_tools.console.observation_view import ObservationView
from piksi_tools.console.sbp_relay_view import SbpRelayView
from piksi_tools.console.system_monitor_view import SystemMonitorView
from piksi_tools.console.settings_view import SettingsView
from piksi_tools.console.update_view import UpdateView
from enable.savage.trait_defs.ui.svg_button import SVGButton

CONSOLE_TITLE = 'Piksi Console, Version: v' + CONSOLE_VERSION


class ConsoleHandler(Handler):
  """
  Handler that updates the window title with the device serial number

  This Handler is used by Traits UI to manage making changes to the GUI in
  response to changes in the underlying class/data.
  """

  def object_device_serial_changed(self, info):
    """
    Update the window title with the device serial number.

    This is a magic method called by the handler in response to any changes in
    the `device_serial` variable in the underlying class.
    """
    if info.initialized:
      info.ui.title = CONSOLE_TITLE + ' : ' + info.object.device_serial


class SwiftConsole(HasTraits):
  """Traits-defined Swift Console.

  link : object
    Serial driver
  update : bool
    Update the firmware
  log_level_filter : str
    Syslog string, one of "ERROR", "WARNING", "INFO", "DEBUG".
  skip_settings : bool
    Don't read the device settings. Set to False when the console is reading
    from a network connection only.

  """

  link = Instance(sbpc.Handler)
  console_output = Instance(OutputList())
  python_console_env = Dict
  device_serial = Str('')
  a = Int
  b = Int
  tracking_view = Instance(TrackingView)
  solution_view = Instance(SolutionView)
  baseline_view = Instance(BaselineView)
  observation_view = Instance(ObservationView)
  networking_view = Instance(SbpRelayView)
  observation_view_base = Instance(ObservationView)
  system_monitor_view = Instance(SystemMonitorView)
  settings_view = Instance(SettingsView)
  update_view = Instance(UpdateView)
  log_level_filter = Enum(list(SYSLOG_LEVELS.itervalues()))

  paused_button = SVGButton(
    label='', tooltip='Pause console update', toggle_tooltip='Resume console update', toggle=True,
    filename=os.path.join(determine_path(), 'images', 'iconic', 'pause.svg'),
    toggle_filename=os.path.join(determine_path(), 'images', 'iconic', 'play.svg'),
    width=8, height=8
  )
  clear_button = SVGButton(
    label='', tooltip='Clear console buffer',
    filename=os.path.join(determine_path(), 'images', 'iconic', 'x.svg'),
    width=8, height=8
  )

  view = View(
    VSplit(
      Tabbed(
        Item('tracking_view', style='custom', label='Tracking'),
        Item('solution_view', style='custom', label='Solution'),
        Item('baseline_view', style='custom', label='Baseline'),
        VSplit(
          Item('observation_view', style='custom', show_label=False),
          Item('observation_view_base', style='custom', show_label=False),
          label='Observations',
        ),
        Item('settings_view', style='custom', label='Settings'),
        Item('update_view', style='custom', label='Firmware Update'),
        Tabbed(
          Item('system_monitor_view', style='custom', label='System Monitor'),
          Item('networking_view', label='Networking', style='custom', show_label=False),
          Item(
            'python_console_env', style='custom',
            label='Python Console', editor=ShellEditor()),
          label='Advanced',
          show_labels=False
         ),
        show_labels=False
      ),
      VGroup(
        HGroup(
          Spring(width=4, springy=False),
          Item('paused_button', show_label=False, width=8, height=8),
          Item('clear_button', show_label=False, width=8, height=8),
          Item('', label='Console Log', emphasized=True),
          Spring(),
          UItem('log_level_filter', style='simple', padding=0, height=8, show_label=True,
                tooltip='Show log levels up to and including the selected level of severity.\nThe CONSOLE log level is always visible.'),
        ),
        Item(
          'console_output',
          style='custom',
          editor=InstanceEditor(),
          height=0.3,
          show_label=False,
        ),
      )
    ),
    icon = icon,
    resizable = True,
    width = 1000,
    height = 600,
    handler = ConsoleHandler(),
    title = CONSOLE_TITLE
  )

  def print_message_callback(self, sbp_msg, **metadata):
    try:
      encoded = sbp_msg.payload.encode('ascii', 'ignore')
      for eachline in reversed(encoded.split('\n')):
        self.console_output.write_level(eachline,
                                        str_to_log_level(eachline.split(':')[0]))
    except UnicodeDecodeError:
      print "Critical Error encoding the serial stream as ascii."

  def log_message_callback(self, sbp_msg, **metadata):
    try:
      encoded = sbp_msg.text.encode('ascii', 'ignore')
      for eachline in reversed(encoded.split('\n')):
        self.console_output.write_level(eachline, sbp_msg.level)
    except UnicodeDecodeError:
      print "Critical Error encoding the serial stream as ascii."

  def ext_event_callback(self, sbp_msg, **metadata):
    e = MsgExtEvent(sbp_msg)
    print 'External event: %s edge on pin %d at wn=%d, tow=%d, time qual=%s' % (
      "Rising" if (e.flags & (1<<0)) else "Falling", e.pin, e.wn, e.tow,
      "good" if (e.flags & (1<<1)) else "unknown")

  def _paused_button_fired(self):
    self.console_output.paused = not self.console_output.paused

  def _log_level_filter_changed(self):
    """
    Takes log level enum and translates into the mapped integer.
    Integer stores the current filter value inside OutputList.
    """
    self.console_output.log_level_filter = str_to_log_level(self.log_level_filter)

  def _clear_button_fired(self):
    self.console_output.clear()

  def __init__(self, link, update, log_level_filter, skip_settings=False):
    self.console_output = OutputList()
    self.console_output.write("Console: starting...")
    sys.stdout = self.console_output
    sys.stderr = self.console_output
    self.log_level_filter = log_level_filter
    self.console_output.log_level_filter = str_to_log_level(log_level_filter)
    try:
      self.link = link
      self.link.add_callback(self.print_message_callback, SBP_MSG_PRINT_DEP)
      self.link.add_callback(self.log_message_callback, SBP_MSG_LOG)
      self.link.add_callback(self.ext_event_callback, SBP_MSG_EXT_EVENT)
      self.dep_handler = DeprecatedMessageHandler(link)
      settings_read_finished_functions = []
      self.tracking_view = TrackingView(self.link)
      self.solution_view = SolutionView(self.link)
      self.baseline_view = BaselineView(self.link)
      self.observation_view = ObservationView(self.link, name='Rover', relay=False)
      self.observation_view_base = ObservationView(self.link, name='Base', relay=True)
      self.system_monitor_view = SystemMonitorView(self.link)
      self.update_view = UpdateView(self.link, prompt=update)
      settings_read_finished_functions.append(self.update_view.compare_versions)
      self.networking_view = SbpRelayView(self.link)
      # Once we have received the settings, update device_serial with
      # the Piksi serial number which will be displayed in the window
      # title. This callback will also update the header route as used
      # by the networking view.
      def update_serial():
        serial_string = self.settings_view.settings['system_info']['serial_number'].value
        self.device_serial = 'PK%04d' % int(serial_string)
        if serial_string:
          self.networking_view.set_route(int(serial_string))
      settings_read_finished_functions.append(update_serial)
      self.settings_view = SettingsView(self.link,
                                        settings_read_finished_functions,
                                        hide_expert=not args.expert,
                                        skip=skip_settings)
      self.update_view.settings = self.settings_view.settings
      self.python_console_env = { 'send_message': self.link,
                                  'link': self.link, }
      self.python_console_env.update(self.tracking_view.python_console_cmds)
      self.python_console_env.update(self.solution_view.python_console_cmds)
      self.python_console_env.update(self.baseline_view.python_console_cmds)
      self.python_console_env.update(self.observation_view.python_console_cmds)
      self.python_console_env.update(self.networking_view.python_console_cmds)
      self.python_console_env.update(self.system_monitor_view.python_console_cmds)
      self.python_console_env.update(self.update_view.python_console_cmds)
      self.python_console_env.update(self.settings_view.python_console_cmds)
    except:
      import traceback
      traceback.print_exc()

# Make sure that SIGINT (i.e. Ctrl-C from command line) actually stops the
# application event loop (otherwise Qt swallows KeyboardInterrupt exceptions)
signal.signal(signal.SIGINT, signal.SIG_DFL)

# If using a device connected to an actual port, then invoke the
# regular console dialog for port selection
class PortChooser(HasTraits):
  ports = List()
  port = Str(None)
  traits_view = View(
    VGroup(
      Label('Select Piksi device:'),
      Item('port', editor=EnumEditor(name='ports'), show_label=False),
    ),
    buttons = ['OK', 'Cancel'],
    close_result=False,
    icon = icon,
    width = 250,
    title = 'Select serial device',
  )

  def __init__(self):
    try:
      self.ports = [p for p, _, _ in s.get_ports()]
    except TypeError:
      pass

if not port:
  port_chooser = PortChooser()
  is_ok = port_chooser.configure_traits()
  port = port_chooser.port
  if not port or not is_ok:
    print "No serial device selected!"
    sys.exit(1)
  else:
    print "Using serial device '%s'" % port

with s.get_driver(args.ftdi, port, baud) as driver:
  with sbpc.Handler(sbpc.Framer(driver.read, driver.write, args.verbose)) as link:
    if os.path.isdir(log_filename):
      log_filename = os.path.join(log_filename, s.LOG_FILENAME)
    with s.get_logger(args.log, log_filename) as logger:
      if args.reset:
        link(MsgReset())
      sbpc.Forwarder(link, logger).start()
      log_filter = DEFAULT_LOG_LEVEL_FILTER
      if args.initloglevel[0]:
        log_filter = args.initloglevel[0]
      SwiftConsole(link, args.update, log_filter).configure_traits()

# Force exit, even if threads haven't joined
try:
  os._exit(0)
except:
  pass
