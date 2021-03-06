#!/usr/bin/env python
# Copyright (C) 2014 Swift Navigation Inc.
# Contact: Colin Beighley <colin@swift-nav.com>
#
# This source is subject to the license found in the file 'LICENSE' which must
# be be distributed together with this source. All other rights reserved.
#
# THIS CODE AND INFORMATION IS PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND,
# EITHER EXPRESSED OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND/OR FITNESS FOR A PARTICULAR PURPOSE.

from urllib2 import URLError
from time import sleep
from intelhex import IntelHex, HexRecordError
from pkg_resources import parse_version

from sbp.bootload import MsgBootloaderJumpToApp
from sbp.piksi import MsgReset

from threading import Thread

from traits.api import HasTraits, String, Button, Instance, Bool
from traitsui.api import View, Item, UItem, VGroup, HGroup, InstanceEditor
from pyface.api import GUI, FileDialog, OK, ProgressDialog

from piksi_tools.version import VERSION as CONSOLE_VERSION
from piksi_tools import bootload
from piksi_tools import flash
import piksi_tools.console.callback_prompt as prompt
from piksi_tools.console.utils import determine_path

from update_downloader import UpdateDownloader
from output_stream import OutputStream

import sys, os
from pyface.image_resource import ImageResource
if getattr(sys, 'frozen', False):
    # we are running in a |PyInstaller| bundle
    basedir = sys._MEIPASS
    os.chdir(basedir)
else:
    # we are running in a normal Python environment
    basedir = determine_path()
icon = ImageResource('icon',
         search_path=['images', os.path.join(basedir, 'images')])

INDEX_URL = 'http://downloads.swiftnav.com/index.json'
HT = 8
COLUMN_WIDTH = 100

class IntelHexFileDialog(HasTraits):

  file_wildcard = String("Intel HEX File (*.hex)|*.hex|All files|*")

  status = String('Please choose a file')
  choose_fw = Button(label='...', padding=-1)
  view = View(
               HGroup(UItem('status', resizable=True),
                      UItem('choose_fw', width=-0.1)),
             )

  def __init__(self, flash_type):
    """
    Pop-up file dialog to choose an IntelHex file, with status and button to
    display in traitsui window.

    Parameters
    ----------
    flash_type : string
      Which Piksi flash to interact with ("M25" or "STM").
    """
    if not flash_type == 'M25' and not flash_type == 'STM':
      raise ValueError("flash_type must be 'M25' or 'STM'")
    self._flash_type = flash_type
    self.ihx = None

  def clear(self, status):
    """
    Set text of status box and clear IntelHex file.

    Parameters
    ----------
    status : string
      Error text to replace status box text with.
    """
    self.ihx = None
    self.status = status

  def load_ihx(self, filepath):
    """
    Load IntelHex file and set status to indicate if file was
    successfully loaded.

    Parameters
    ----------
    filepath : string
      Path to IntelHex file.
    """
    try:
      self.ihx = IntelHex(filepath)
      self.status = os.path.split(filepath)[1]
    except HexRecordError:
      self.clear('Error: File is not a valid Intel HEX File')

    # Check that address ranges are valid for self._flash_type.
    ihx_addrs = flash.ihx_ranges(self.ihx)
    if self._flash_type == "M25":
      try:
        sectors = flash.sectors_used(ihx_addrs, flash.m25_addr_sector_map)
      except IndexError:
        self.clear('Error: HEX File contains restricted address ' + \
                        '(STM Firmware File Chosen?)')
    elif self._flash_type == "STM":
      try:
        sectors = flash.sectors_used(ihx_addrs, flash.stm_addr_sector_map)
      except:
        self.clear('Error: HEX File contains restricted address ' + \
                        '(NAP Firmware File Chosen?)')

  def _choose_fw_fired(self):
    """ Activate file dialog window to choose IntelHex firmware file. """
    dialog = FileDialog(label='Choose Firmware File',
                        action='open', wildcard=self.file_wildcard)
    dialog.open()
    if dialog.return_code == OK:
      filepath = os.path.join(dialog.directory, dialog.filename)
      self.load_ihx(filepath)
    else:
      self.clear('Error while selecting file')

class PulsableProgressDialog(ProgressDialog):

  def __init__(self, max, pulsed=False):
    """
    Pop-up window for showing a process's progress.

    Parameters
    ----------
    max : int
      Maximum value of the progress bar.
    pulsed : bool
      Show non-partial progress initially.
    """
    super(PulsableProgressDialog, self).__init__()
    self.min = 0
    self.max = 0
    self.pulsed = pulsed
    self.passed_max = max

  def progress(self, count):
    """
    Update progress of progress bar. If pulsing initially, wait until count
    is at least 12 before changing to discrete progress bar.

    Parameters
    ----------
    count : int
      Current value of progress.
    """
    # Provide user feedback initially via pulse for slow sector erases.
    if self.pulsed:
      if count > 12:
        self.max = 100
        GUI.invoke_later(self.update, int(100*float(count)/self.passed_max))
    else:
      self.max = 100
      GUI.invoke_later(self.update, int(100*float(count)/self.passed_max))

  def close(self):
    """ Close progress bar window. """
    GUI.invoke_after(0.1, super(PulsableProgressDialog, self).close)
    sleep(0.2)

class UpdateView(HasTraits):

  piksi_stm_vers = String('Waiting for Piksi to send settings...', width=COLUMN_WIDTH)
  newest_stm_vers = String('Downloading Newest Firmware info...')
  piksi_nap_vers = String('Waiting for Piksi to send settings...')
  newest_nap_vers = String('Downloading Newest Firmware info...')
  local_console_vers = String('v' + CONSOLE_VERSION)
  newest_console_vers = String('Downloading Newest Console info...')

  erase_stm = Bool(True)
  erase_en = Bool(True)

  update_stm_firmware = Button(label='Update STM')
  update_nap_firmware = Button(label='Update NAP')
  update_full_firmware = Button(label='Update Piksi STM and NAP Firmware')

  updating = Bool(False)
  update_stm_en = Bool(False)
  update_nap_en = Bool(False)
  update_en = Bool(False)

  download_firmware = Button(label='Download Newest Firmware Files')
  download_stm = Button(label='Download', height=HT)
  download_nap = Button(label='Download', height=HT)
  downloading = Bool(False)
  download_fw_en = Bool(True)

  stm_fw = Instance(IntelHexFileDialog)
  nap_fw = Instance(IntelHexFileDialog)

  stream = Instance(OutputStream)

  view = View(
    VGroup(
      HGroup(
        VGroup(
          Item('piksi_stm_vers', label='Current', resizable=True),
          Item('newest_stm_vers', label='Latest', resizable=True),
          Item('stm_fw', style='custom', show_label=True, \
               label="Local File", enabled_when='download_fw_en'),
          HGroup(Item('update_stm_firmware', show_label=False, \
                     enabled_when='update_stm_en'),
                Item('erase_stm', label='Erase STM flash\n(recommended)', \
                      enabled_when='erase_en', show_label=True)),
          show_border=True, label="STM Firmware Version"
        ),
        VGroup(
          Item('piksi_nap_vers', label='Current', resizable=True),
          Item('newest_nap_vers', label='Latest', resizable=True),
          Item('nap_fw', style='custom', show_label=True, \
               label="Local File", enabled_when='download_fw_en'),
          HGroup(Item('update_nap_firmware', show_label=False, \
                      enabled_when='update_nap_en'),
                 Item(width=50, label="                  ")),
          show_border=True, label="NAP Firmware Version"
          ),
        VGroup(
          Item('local_console_vers', label='Current', resizable=True),
          Item('newest_console_vers', label='Latest'),
          label="Piksi Console Version", show_border=True),
          ),
      UItem('download_firmware', enabled_when='download_fw_en'),
      UItem('update_full_firmware', enabled_when='update_en'),
      Item(
        'stream',
        style='custom',
        editor=InstanceEditor(),
        show_label=False,
      ),
    )
  )

  def __init__(self, link, prompt=True):
    """
    Traits tab with UI for updating Piksi firmware.

    Parameters
    ----------
    link : sbp.client.handler.Handler
      Link for SBP transfer to/from Piksi.
    prompt : bool
      Prompt user to update console/firmware if out of date.
    """
    self.link = link
    self.settings = {}
    self.prompt = prompt
    self.python_console_cmds = {
      'update': self

    }
    self.update_dl = None
    self.erase_en = True
    self.stm_fw = IntelHexFileDialog('STM')
    self.stm_fw.on_trait_change(self._manage_enables, 'status')
    self.nap_fw = IntelHexFileDialog('M25')
    self.nap_fw.on_trait_change(self._manage_enables, 'status')
    self.stream = OutputStream()
    self.get_latest_version_info()

  def _manage_enables(self):
    """ Manages whether traits widgets are enabled in the UI or not. """
    if self.updating == True or self.downloading == True:
      self.update_stm_en = False
      self.update_nap_en = False
      self.update_en = False
      self.download_fw_en = False
      self.erase_en = False
    else:
      self.download_fw_en = True
      self.erase_en = True
      if self.stm_fw.ihx is not None:
        self.update_stm_en = True
      else:
        self.update_stm_en = False
        self.update_en = False
      if self.nap_fw.ihx is not None:
        self.update_nap_en = True
      else:
        self.update_nap_en = False
        self.update_en = False
      if self.nap_fw.ihx is not None and self.stm_fw.ihx is not None:
        self.update_en = True

  def _updating_changed(self):
    """ Handles self.updating trait being changed. """
    self._manage_enables()

  def _downloading_changed(self):
    """ Handles self.downloading trait being changed. """
    self._manage_enables()

  def _write(self, text):
    """
    Stream style write function. Allows flashing debugging messages to be
    routed to embedded text console.

    Parameters
    ----------
    text : string
      Text to be written to screen.
    """
    self.stream.write(text)
    self.stream.write('\n')
    self.stream.flush()

  def _update_stm_firmware_fired(self):
    """
    Handle update_stm_firmware button. Starts thread so as not to block the GUI
    thread.
    """
    try:
      if self._firmware_update_thread.is_alive():
        return
    except AttributeError:
      pass

    self._firmware_update_thread = Thread(target=self.manage_firmware_updates,
                                          args=("STM",))
    self._firmware_update_thread.start()

  def _update_nap_firmware_fired(self):
    """
    Handle update_nap_firmware button. Starts thread so as not to block the GUI
    thread.
    """
    try:
      if self._firmware_update_thread.is_alive():
        return
    except AttributeError:
      pass

    self._firmware_update_thread = Thread(target=self.manage_firmware_updates,
                                          args=("M25",))
    self._firmware_update_thread.start()

  def _update_full_firmware_fired(self):
    """
    Handle update_full_firmware button. Starts thread so as not to block the GUI
    thread.
    """
    try:
      if self._firmware_update_thread.is_alive():
        return
    except AttributeError:
      pass

    self._firmware_update_thread = Thread(target=self.manage_firmware_updates,
                                          args=("ALL",))
    self._firmware_update_thread.start()

  def _download_firmware(self):
    """ Download latest firmware from swiftnav.com. """
    self._write('')

    # Check that we received the index file from the website.
    if self.update_dl == None:
      self._write("Error: Can't download firmware files")
      return

    self.downloading = True

    status = 'Downloading Newest Firmware...'
    self.nap_fw.clear(status)
    self.stm_fw.clear(status)
    self._write(status)

    # Get firmware files from Swift Nav's website, save to disk, and load.
    try:
      self._write('Downloading Newest NAP firmware')
      filepath = self.update_dl.download_nap_firmware()
      self._write('Saved file to %s' % filepath)
      self.nap_fw.load_ihx(filepath)
    except AttributeError:
      self.nap_fw.clear("Error downloading firmware")
      self._write("Error downloading firmware: index file not downloaded yet")
    except KeyError:
      self.nap_fw.clear("Error downloading firmware")
      self._write("Error downloading firmware: URL not present in index")
    except URLError:
      self.nap_fw.clear("Error downloading firmware")
      self._write("Error: Failed to download latest NAP firmware from Swift Navigation's website")

    try:
      self._write('Downloading Newest STM firmware')
      filepath = self.update_dl.download_stm_firmware()
      self._write('Saved file to %s' % filepath)
      self.stm_fw.load_ihx(filepath)
    except AttributeError:
      self.stm_fw.clear("Error downloading firmware")
      self._write("Error downloading firmware: index file not downloaded yet")
    except KeyError:
      self.stm_fw.clear("Error downloading firmware")
      self._write("Error downloading firmware: URL not present in index")
    except URLError:
      self.stm_fw.clear("Error downloading firmware")
      self._write("Error: Failed to download latest STM firmware from Swift Navigation's website")

    self.downloading = False

  def _download_firmware_fired(self):
    """
    Handle download_firmware button. Starts thread so as not to block the GUI
    thread.
    """
    try:
      if self._download_firmware_thread.is_alive():
        return
    except AttributeError:
      pass

    self._download_firmware_thread = Thread(target=self._download_firmware)
    self._download_firmware_thread.start()

  def compare_versions(self):
    """
    To be called after latest Piksi firmware info has been received from
    device, to decide if current firmware on Piksi is out of date. Starts a
    thread so as not to block GUI thread.
    """
    try:
      if self._compare_versions_thread.is_alive():
        return
    except AttributeError:
      pass

    self._compare_versions_thread = Thread(target=self._compare_versions)
    self._compare_versions_thread.start()

  def _compare_versions(self):
    """
    Compares version info between received firmware version / current console
    and firmware / console info from website to decide if current firmware or
    console is out of date. Prompt user to update if so.
    """
    # Check that settings received from Piksi contain FW versions.
    try:
      self.piksi_stm_vers = \
        self.settings['system_info']['firmware_version'].value
      self.piksi_nap_vers = \
        self.settings['system_info']['nap_version'].value
    except KeyError:
      self._write("\nError: Settings received from Piksi don't contain firmware version keys. Please contact Swift Navigation.\n")
      return

    # Check that we received the index file from the website.
    if self.update_dl == None:
      self._write("Error: No website index to use to compare versions with local firmware")
      return

    # Check if console is out of date and notify user if so.
    if self.prompt:
      local_console_version = parse_version(CONSOLE_VERSION)
      remote_console_version = parse_version(self.newest_console_vers)
      self.console_outdated = remote_console_version > local_console_version

      if self.console_outdated:
        console_outdated_prompt = \
            prompt.CallbackPrompt(
                                  title="Piksi Console Outdated",
                                  actions=[prompt.close_button],
                                 )

        console_outdated_prompt.text = \
            "Your Piksi Console is out of date and may be incompatible\n" + \
            "with current firmware. We highly recommend upgrading to\n" + \
            "ensure proper behavior.\n\n" + \
            "Please visit http://downloads.swiftnav.com to\n" + \
            "download the newest version.\n\n" + \
            "Local Console Version :\n\t" + \
                "v" + CONSOLE_VERSION + \
            "\nNewest Console Version :\n\t" + \
                self.update_dl.index['piksi_v2.3.1']['console']['version'] + "\n"

        console_outdated_prompt.run()

    # For timing aesthetics between windows popping up.
    sleep(0.5)

    # Check if firmware is out of date and notify user if so.
    if self.prompt:
      local_stm_version = parse_version(
          self.settings['system_info']['firmware_version'].value)
      remote_stm_version = parse_version(self.newest_stm_vers)

      local_nap_version = parse_version(
          self.settings['system_info']['nap_version'].value)
      remote_nap_version = parse_version(self.newest_nap_vers)

      self.fw_outdated = remote_nap_version > local_nap_version or \
                         remote_stm_version > local_stm_version

      if self.fw_outdated:
        fw_update_prompt = \
            prompt.CallbackPrompt(
                                  title='Firmware Update',
                                  actions=[prompt.close_button]
                                 )

        fw_update_prompt.text = \
            "New Piksi firmware available.\n\n" + \
            "Please use the Firmware Update tab to update.\n\n" + \
            "Newest STM Version :\n\t%s\n\n" % \
                self.update_dl.index['piksi_v2.3.1']['stm_fw']['version'] + \
            "Newest SwiftNAP Version :\n\t%s\n\n" % \
                self.update_dl.index['piksi_v2.3.1']['nap_fw']['version']

        fw_update_prompt.run()

  def get_latest_version_info(self):
    """
    Get latest firmware / console version from website. Starts thread so as not
    to block the GUI thread.
    """
    try:
      if self._get_latest_version_info_thread.is_alive():
        return
    except AttributeError:
      pass

    self._get_latest_version_info_thread = Thread(target=self._get_latest_version_info)
    self._get_latest_version_info_thread.start()

  def _get_latest_version_info(self):
    """ Get latest firmware / console version from website. """
    try:
      self.update_dl = UpdateDownloader()
    except URLError:
      self._write("\nError: Failed to download latest file index from Swift Navigation's website. Please visit our website to check that you're running the latest Piksi firmware and Piksi console.\n")
      return

    # Make sure index contains all keys we are interested in.
    try:
      self.newest_stm_vers = self.update_dl.index['piksi_v2.3.1']['stm_fw']['version']
      self.newest_nap_vers = self.update_dl.index['piksi_v2.3.1']['nap_fw']['version']
      self.newest_console_vers = self.update_dl.index['piksi_v2.3.1']['console']['version']
    except KeyError:
      self._write("\nError: Index downloaded from Swift Navigation's website (%s) doesn't contain all keys. Please contact Swift Navigation.\n" % INDEX_URL)
      return

  def manage_stm_firmware_update(self):
    # Erase all of STM's flash (other than bootloader) if box is checked.
    if self.erase_stm:
      text = "Erasing STM"
      self._write(text)
      self.create_flash("STM")
      sectors_to_erase = set(range(self.pk_flash.n_sectors)).difference(set(self.pk_flash.restricted_sectors))
      progress_dialog = PulsableProgressDialog(len(sectors_to_erase), False)
      progress_dialog.title = text
      GUI.invoke_later(progress_dialog.open)
      erase_count = 0
      for s in sorted(sectors_to_erase):
        progress_dialog.progress(erase_count)
        self._write('Erasing %s sector %d' % (self.pk_flash.flash_type,s))
        self.pk_flash.erase_sector(s)
        erase_count += 1
      self.stop_flash()
      self._write("")
      progress_dialog.close()

    # Flash STM.
    text = "Updating STM"
    self._write(text)
    self.create_flash("STM")
    stm_n_ops = self.pk_flash.ihx_n_ops(self.stm_fw.ihx, \
                                        erase = not self.erase_stm)
    progress_dialog = PulsableProgressDialog(stm_n_ops, True)
    progress_dialog.title = text
    GUI.invoke_later(progress_dialog.open)
    # Don't erase sectors if we've already done so above.
    self.pk_flash.write_ihx(self.stm_fw.ihx, self.stream, mod_print=0x40, \
                            elapsed_ops_cb = progress_dialog.progress, \
                            erase = not self.erase_stm)
    self.stop_flash()
    self._write("")
    progress_dialog.close()

  def manage_nap_firmware_update(self, check_version=False):
    # Flash NAP if out of date.
    try:
      local_nap_version = parse_version(
          self.settings['system_info']['nap_version'].value)
      remote_nap_version = parse_version(self.newest_nap_vers)
      nap_out_of_date = local_nap_version != remote_nap_version
    except KeyError:
      nap_out_of_date = True
    if nap_out_of_date or check_version==False:
      text = "Updating NAP"
      self._write(text)
      self.create_flash("M25")
      nap_n_ops = self.pk_flash.ihx_n_ops(self.nap_fw.ihx)
      progress_dialog = PulsableProgressDialog(nap_n_ops, True)
      progress_dialog.title = text
      GUI.invoke_later(progress_dialog.open)
      self.pk_flash.write_ihx(self.nap_fw.ihx, self.stream, mod_print=0x40, \
                              elapsed_ops_cb = progress_dialog.progress)
      self.stop_flash()
      self._write("")
      progress_dialog.close()
      return True
    else:
      text = "NAP is already to latest version, not updating!"
      self._write(text)
      self._write("")
      return False

  # Executed in GUI thread, called from Handler.
  def manage_firmware_updates(self, device):
    """
    Update Piksi firmware. Erase entire STM flash (other than bootloader)
    if so directed. Flash NAP only if new firmware is available.
    """
    self.updating = True
    update_nap = False
    self._write('')

    if device == "STM":
      self.manage_stm_firmware_update()
    elif device == "M25":
      update_nap = self.manage_nap_firmware_update()
    else:
      self.manage_stm_firmware_update()
      update_nap = self.manage_nap_firmware_update(check_version=True)

    # Must tell Piksi to jump to application after updating firmware.
    if device == "STM" or update_nap:
        self.link(MsgBootloaderJumpToApp(jump=0))
        self._write("Firmware update finished.")
        self._write("")

    self.updating = False

  def create_flash(self, flash_type):
    """
    Create flash.Flash instance and set Piksi into bootloader mode, prompting
    user to reset if necessary.

    Parameter
    ---------
    flash_type : string
      Either "STM" or "M25".
    """
    # Reset device if the application is running to put into bootloader mode.
    self.link(MsgReset())

    self.pk_boot = bootload.Bootloader(self.link)

    self._write("Waiting for bootloader handshake message from Piksi ...")
    reset_prompt = None
    handshake_received = self.pk_boot.handshake(1)

    # Prompt user to reset Piksi if we don't receive the handshake message
    # within a reasonable amount of tiime (firmware might be corrupted).
    while not handshake_received:
      reset_prompt = \
        prompt.CallbackPrompt(
                              title="Please Reset Piksi",
                              actions=[prompt.close_button],
                             )

      reset_prompt.text = \
            "You must press the reset button on your Piksi in order\n" + \
            "to update your firmware.\n\n" + \
            "Please press it now.\n\n"

      reset_prompt.run(block=False)

      while not reset_prompt.closed and not handshake_received:
        handshake_received = self.pk_boot.handshake(1)

      reset_prompt.kill()
      reset_prompt.wait()

    self._write("received bootloader handshake message.")
    self._write("Piksi Onboard Bootloader Version: " + self.pk_boot.version)

    self.pk_flash = flash.Flash(self.link, flash_type, self.pk_boot.sbp_version)

  def stop_flash(self):
    """
    Stop Flash and Bootloader instances (removes callback from SerialLink).
    """
    self.pk_flash.stop()
    self.pk_boot.stop()
