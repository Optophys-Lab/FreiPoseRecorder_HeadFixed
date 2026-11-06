"""
Tool to record videos from multiple Basler cameras synchronously. Based on RecTool by Christian.

Author: Artur artur.schneider@biologie.uni-freiburg.de
"""

import datetime
import json
import logging
import sys
import time
import shutil

from queue import Empty
from threading import Event, Thread

from PyQt6.QtWidgets import QApplication, QMainWindow, QFileDialog, QMessageBox
from PyQt6.QtCore import QTimer
from PyQt6 import uic, QtGui, QtSerialPort

from pathlib import Path
from FreiPose_Recorder.core.Recorder import Recorder
from FreiPose_Recorder.ImageViewer import SingleCamViewer, RemoteConnDialog
from FreiPose_Recorder.utils.socket_utils import SocketComm, SocketMessage, MessageType
from FreiPose_Recorder.configs.params import *
from FreiPose_Recorder.utils.serial_utils import QtPicoSerial

log = logging.getLogger('main')
log.setLevel(logging.DEBUG)

#make the logging to file
log_path = Path('logs')
log_path.mkdir(exist_ok=True)
if LOG2FILE:
    logging.basicConfig(filename=log_path / f'GUI_run{datetime.datetime.now().strftime("%m%d_%H%M")}.log', filemode='w',
                        format='%(asctime)s - %(levelname)s - %(message)s')

VERSION = "0.5.1"

# TODO
# move camera enums somewhere less convoluted
# check correct distribution of frames in the corresponding Qs!
# Write documentation for the functions make a docs for it
# implement connection to raspberry pico (scan ports etc)

class BASLER_GUI(QMainWindow):
    def __init__(self):
        super(BASLER_GUI, self).__init__()
        self.MultiViewWidget = None  # is loaded from the GUI_design.ui
        self.CameraSettings = None   # is loaded from the GUI_design.ui
        self.session_path = None  # path to the current session
        self.files_copied = False  # flag to check if files have been copied
        self.timer_update_counter = 0
        self.rec_start_time = None  # time when recording started
        self.calib_start_timer = None
        self.calib_stop_timer = None
        self.trigger_timer = None
        self.session_id = "test_sess"
        self.single_camviewer = None
        self.multi_view_timer = None
        self.stop_event = None
        self.single_view_timer = None
        self.path2file = Path(__file__)
        uic.loadUi(self.path2file.parent / 'GUI' / 'GUI_design.ui', self)
        self.setWindowTitle(f'FreiPose Recorder v.{VERSION}')
        self.log = logging.getLogger('GUI')
        self.log.setLevel(logging.DEBUG)

        self.Codec_comboBox.addItems(codec_to_try)

        #Setting icons for some buttons for prettiness
        self.RUNButton.setIcon(QtGui.QIcon("GUI/icons/play.svg"))
        self.RECButton.setIcon(QtGui.QIcon("GUI/icons/record.svg"))
        self.STOPButton.setIcon(QtGui.QIcon("GUI/icons/stop.svg"))
        self.RemoteModeButton.setIcon(QtGui.QIcon("GUI/icons/Signal.svg"))
        self.ConnectButton.setIcon(QtGui.QIcon("GUI/icons/connect.svg"))
        self.Save_pathButton.setIcon(QtGui.QIcon("GUI/icons/folder.svg"))
        self.SettingsSaveButton.setIcon(QtGui.QIcon("GUI/icons/DocumentArrowDown.svg"))
        self.SettingsLoadButton.setIcon(QtGui.QIcon("GUI/icons/DocumentArrowUp.svg"))
        self.ShowSingleCamButton.setIcon(QtGui.QIcon("GUI/icons/Camera.svg"))
        self.FlipXButton.setIcon(QtGui.QIcon("GUI/icons/ArrowsRightLeft.svg"))
        self.FlipYButton.setIcon(QtGui.QIcon("GUI/icons/ArrowsUpDown.svg"))
        self.Rec_status.setPixmap(QtGui.QIcon("GUI/icons/VideoCameraSlash.svg").pixmap(64))

        self.ConnectSignals()
        self.basler_recorder = Recorder(write_timestamps=SAVE_TIMESTAMPS)
        self.scan_cams()
        if AUTO_CONNECT_CAMS:
            self.connect_to_cams()

        if ENABLE_REMOTE:
            self.socket_comm = SocketComm(type='server', host=HOST, port=PORT)
        else:  # disable remote mode
            self.socket_comm = None
            self.RemoteModeButton.deleteLater()
            self.Client_label.deleteLater()

        self.is_remote_ctr = False  # bool whether the GUI is currently in remote mode

        if USE_ARDUINO_TRIGGER:  # NOT IMPLEMENTE
            #serial_port = f"/dev/{QtSerialPort.QSerialPortInfo.availablePorts()[0].portName()}"
            # find a way to get the port name on windows machines
            #self.trigger = TriggerArduino(serial_port)
            self.scan_ports()
            self.trigger = QtPicoSerial(self)
            #raise NotImplementedError('Arduino trigger not implemented')
            if AUTO_CONNECT_COM:  # automatic connect to default com
                self.PortsCombo.setCurrentText(DEFAULT_COM_PORT)
                self.connect_to_pico()
        else:
            self.trigger = None
            self.PortsCombo.deleteLater()
            self.ConnectB.deleteLater()
            self.PingB.deleteLater()
            self.DisConnectB.deleteLater()

    ### Serial connectivity ####
    def scan_ports(self):
        """scans for available serial ports"""
        self.log.debug(f'Scanning serial ports')
        self.PortsCombo.clear()
        self.PortsCombo.addItem("<no port selected>")
        for port in QtSerialPort.QSerialPortInfo.availablePorts():
            self.PortsCombo.insertItem(0, port.portName())
        self.ConnectB.setEnabled(True)
        potential_port = [p_id for p_id, port in enumerate(QtSerialPort.QSerialPortInfo.availablePorts())
                          if port.portName() == 'ttyACM1']
        for port_id in potential_port:
            self.PortsCombo.setCurrentIndex(port_id + 1)

    def connect_to_pico(self):
        """Connects to the serial port"""
        portname = self.PortsCombo.currentText()
        if portname == "<no port selected>":
            self.log.info('No serial port chosen')
            return
        if  self.trigger is None:
            return
        self.trigger.set_port(portname)
        success = self.trigger.open()
        if success:
            self.log.debug(f'Connected to port {portname}')
            self.ConnectB.setText("Connected")
            self.ConnectB.setEnabled(False)
            self.PortsCombo.setEnabled(False)
            self.DisConnectB.setEnabled(True)

    def disconnect_from_pico(self):
        """Disconnects from the serial port"""
        if  self.trigger is None:
            return
        self.trigger.close()
        self.log.debug('Disconnected')
        self.ConnectB.setText("Connect")
        self.ConnectB.setEnabled(True)
        self.PortsCombo.setEnabled(True)
        self.DisConnectB.setEnabled(False)
        self.scan_ports()

    def pingPython(self):
        """triggers the pythoncommunicator to send ping command"""
        self.trigger.ping()
        self.log.debug('Pinging Circuitpython')

    def reset_ping(self):
        """
        Waits for some time and then turns the PingB back to normal.
        """
        time.sleep(5)
        self.PingB.setStyleSheet('')

    def pico_data_received(self, payload):
        """Process a message from the Pico."""
        payload = payload.decode()
        self.log.debug("Received: %s", payload)
        self.parse_message(payload)

    def parse_message(self, message):
        m_type = message.split('_')[0]
        try:
            payload = message.split('_')[1]  # make sure there is something here!
        except IndexError:
            # message without payload
            payload = None

        if m_type == 'PONG':  # Board responded
            self.PingB.setStyleSheet('QPushButton {background-color: green;}')
            self.reset_ping_thread = Thread(target=self.reset_ping)
            self.reset_ping_thread.start()

    ### Device Connectivity ####
    def scan_cams(self):
        found_cams = self.basler_recorder.get_cam_info()
        nr_cams = len(found_cams)
        if nr_cams > 0:
            found_cams = '\n'.join(found_cams)
            self.Devices_textEdit.clear()
            self.Devices_textEdit.setText(f"Found cameras SN:\n{found_cams}")

            self.ConnectButton.setEnabled(True)
            self.ScanDevButton.setEnabled(False)  #Y only scan once ?
        else:
            self.Devices_textEdit.clear()
            self.Devices_textEdit.setText(f"Found no cameras !!\n (Re-)Connect a camera and try again.")

        self.MultiViewWidget.num_cameras = nr_cams
        self.CameraSettings.num_cameras = nr_cams

    def connect_to_cams(self):
        self.basler_recorder.connect_cams()

        for c_id, cam in enumerate(self.basler_recorder.cam_array):
            self.CameraSettings.toolbox.setItemText(c_id, cam.DeviceInfo.GetUserDefinedName())
            self.CameraSettings.exposure_spin_list[c_id].blockSignals(True)  # block triggering of events
            self.CameraSettings.gain_spin_list[c_id].blockSignals(True)
            self.CameraSettings.color_mode_list[c_id].blockSignals(True)
            self.CameraSettings.exposure_spin_list[c_id].setValue(self.basler_recorder.get_cam_exposureTime(cam))
            self.CameraSettings.gain_spin_list[c_id].setValue(self.basler_recorder.get_cam_gain(cam))
            gain_limits, exp_limits, colormodes = self.basler_recorder.get_cam_limits(cam)
            if exp_limits:
                self.CameraSettings.exposure_spin_list[c_id].setMinimum(exp_limits[0])
                self.CameraSettings.exposure_spin_list[c_id].setMaximum(exp_limits[1])
            if gain_limits:
                self.CameraSettings.gain_spin_list[c_id].setMinimum(gain_limits[0])
                self.CameraSettings.gain_spin_list[c_id].setMaximum(gain_limits[1])
            # add color modes to list
            self.CameraSettings.color_mode_list[c_id].clear()
            self.CameraSettings.color_mode_list[c_id].addItems(colormodes)
            self.CameraSettings.exposure_spin_list[c_id].blockSignals(False)  # unblock triggering of events
            self.CameraSettings.gain_spin_list[c_id].blockSignals(False)
            self.CameraSettings.color_mode_list[c_id].blockSignals(False)

        self.CameraSettings.toolbox.setCurrentIndex(0)
        self.RUNButton.setEnabled(True)
        self.RECButton.setEnabled(True)
        self.REC_calib_Button.setEnabled(True)
        if ENABLE_REMOTE:
            self.RemoteModeButton.setEnabled(True)
        self.ConnectButton.setEnabled(False)

        if AUTO_LOAD_SETTINGS: # load settings after cam connection (if auto load settings = true)
            try:
                self.load_settings(DEFAULT_SETTINGS_FILE)
            except FileNotFoundError:
                self.log.warning(f'Settings file {DEFAULT_SETTINGS_FILE} not found')


    def start_recording(self):
        self.files_copied = False
        self.stop_event = Event()
        session_id = self.SessionIDlineEdit.text()
        if session_id:
            self.session_id = session_id
        self.basler_recorder.fps = self.FrameRateSpin.value()
        self.basler_recorder.codec = self.Codec_comboBox.currentText()
        self.basler_recorder.crf = self.crf_spinBox.value()
        self.number_cams = self.basler_recorder.cam_array.GetSize()
        use_hw_trigger = self.HWTrig_checkBox.isChecked()

        self.basler_recorder.run_multi_cam_record(self.stop_event, filename=self.session_id,
                                                  use_hw_trigger=use_hw_trigger)

        self.multi_view_timer = QTimer()
        self.multi_view_timer.timeout.connect(self.update_multi_view)
        self.multi_view_timer.start(int(1000 // self.basler_recorder.fps))  # match camera frame rate

        self.status_bar_timer = QTimer()
        self.status_bar_timer.timeout.connect(self._update_rec_status_bar)
        self.status_bar_timer.start(1000)  # 1 Hz — queue sizes are readable, not flickering

        self.STOPButton.setEnabled(True)
        self.RUNButton.setEnabled(False)
        self.RECButton.setEnabled(False)
        self.ShowSingleCamButton.setEnabled(False)

        self.AutoExposeButton.setEnabled(False)
        self.AutoGainButton.setEnabled(False)
        self.WhiteBalanceButton.setEnabled(False)
        self.FlipXButton.setEnabled(False)
        self.FlipYButton.setEnabled(False)
        self.CameraSettings.toolbox.setEnabled(False)
        self.All_cams_checkBox.setEnabled(False)
        self.SettingsSaveButton.setEnabled(False)
        self.SettingsLoadButton.setEnabled(False)
        self.FrameRateSpin.setEnabled(False)
        self.Rec_status.setPixmap(QtGui.QIcon("GUI/icons/VideoCamera.svg").pixmap(64))
        # change the pixmap color to red
        self.Rec_status.setStyleSheet("background-color: rgb(255, 0, 0);")

        self.recording_duration_label.setText("0s")
        self.rec_start_time = time.monotonic()
        # create a time that executes the trigger after 500 ms delay to make sure cameras are ready
        if self.trigger and use_hw_trigger:
            self.trigger.fps = self.FrameRateSpin.value()
            self.trigger_timer = QTimer()
            self.trigger_timer.setSingleShot(True)
            self.trigger_timer.timeout.connect(self.trigger.start_trigger)
            self.trigger_timer.start(500)

    def start_recording_calib(self):
        self.HWTrig_checkBox.setChecked(True)  # making sure calib mode is on
        self.log.info(f'Started Calibration sequence..\nwaiting for {CALIB_WAIT}s to start recording')
        # set a timer to start recording with some waiting time
        self.calib_start_timer = QTimer()
        self.calib_start_timer.setSingleShot(True)
        self.calib_start_timer.timeout.connect(self.start_recording)
        self.calib_start_timer.start(CALIB_WAIT)

        # set a timer to stop recording after some time
        self.calib_stop_timer = QTimer()
        self.calib_stop_timer.setSingleShot(True)
        self.calib_stop_timer.timeout.connect(self.stop_cams)
        self.calib_stop_timer.start(CALIB_DURATION + CALIB_WAIT)
        self.log.info(f'Calibration recording will stop after {CALIB_DURATION + CALIB_WAIT}s')
        self.Rec_status.setPixmap(QtGui.QIcon("GUI/icons/VideoCamera.svg").pixmap(64))
        # change the pixmap color to red
        self.Rec_status.setStyleSheet("background-color: rgb(125, 0, 255);")

    def stop_cams(self):
        if self.trigger and self.HWTrig_checkBox.isChecked():
            if  self.trigger.is_pulsing:
                self.trigger.stop_trigger()
                self.trigger_stoptimer = QTimer() # buildin a delay to make sure the trigger stopped
                self.trigger_stoptimer.setSingleShot(True)
                self.trigger_stoptimer.timeout.connect(self.stop_cams)
                self.trigger_stoptimer.start(500)
                return

        if self.stop_event:
            self.stop_event.set()
        self.log.debug('Stopping grabbing')

        if self.calib_start_timer:  # delete timers
            self.calib_start_timer = None
        if self.calib_stop_timer:
            self.calib_stop_timer = None

        # self.basler_recorder.single_view_queue.join() # as this its not being emptied in a thread.. queue is not emptied but stucks here
        if self.single_view_timer:
            self.single_view_timer.stop()
            self.single_view_timer = None
            self.basler_recorder.stop_single_cam_show()

        if hasattr(self, 'status_bar_timer') and self.status_bar_timer:
            self.status_bar_timer.stop()
            self.status_bar_timer = None

        if self.multi_view_timer:
            self.multi_view_timer.stop()
            self.multi_view_timer = None
            if self.basler_recorder.is_recording:
                self.basler_recorder.stop_multi_cam_record()
            else:
                self.basler_recorder.stop_multi_cam_show()

        if self.single_camviewer:
            if self.single_camviewer.isVisible():
                self.single_camviewer.close()
        self.statusbar.showMessage("Stopped Recording")
        # do i want to show remaining images ? not really..
        # maybe instead add an indicator of how many frames are in buffer ?
        self.STOPButton.setEnabled(False)
        self.Rec_status.setPixmap(QtGui.QIcon("GUI/icons/VideoCameraSlash.svg").pixmap(64))
        # change the pixmap color back to none
        self.Rec_status.setStyleSheet("background-color: none")
        if not self.is_remote_ctr:
            self.RUNButton.setEnabled(True)
            self.RECButton.setEnabled(True)
            self.REC_calib_Button.setEnabled(True)

            self.ShowSingleCamButton.setEnabled(True)

            self.AutoExposeButton.setEnabled(True)
            self.AutoGainButton.setEnabled(True)
            self.WhiteBalanceButton.setEnabled(True)
            self.FlipXButton.setEnabled(True)
            self.FlipYButton.setEnabled(True)
            self.CameraSettings.toolbox.setEnabled(True)
            self.All_cams_checkBox.setEnabled(True)
            self.SettingsSaveButton.setEnabled(True)
            self.SettingsLoadButton.setEnabled(True)
            self.FrameRateSpin.setEnabled(True)

        for color_mode in self.CameraSettings.color_mode_list:
            color_mode.setEnabled(True)

    def show_single_cam(self):
        """
        Show single camera in a separate window
        """
        self.All_cams_checkBox.setChecked(False)  # uncheck to not mess up with settings

        current_camid = self.get_current_tab()
        self.stop_event = Event()
        self.basler_recorder.fps = self.FrameRateSpin.value()
        self.basler_recorder.run_single_cam_show(current_camid, self.stop_event)

        self.single_camviewer = SingleCamViewer(self, self.basler_recorder.cam_array[
            current_camid].DeviceInfo.GetUserDefinedName())
        self.single_camviewer.show()

        self.single_view_timer = QTimer()
        self.single_view_timer.timeout.connect(self.update_single_view)

        self.single_view_timer.start(int(1000//(self.FrameRateSpin.value()*1.2)))
        # self.singleview_thread = Thread(target = self.update_single_view)
        # self.singleview_thread.start()
        self.STOPButton.setEnabled(True)
        self.RUNButton.setEnabled(False)
        self.RECButton.setEnabled(False)
        self.ShowSingleCamButton.setEnabled(False)

    def update_single_view(self):
        """
        Updates the single view widget with the current image in the queue
        """
        # stop the whole process if an error occured at basler recorder side
        if self.basler_recorder.error_event.is_set():  # if an error occured
            self.log.error('Error in Basler recorder')
            self.stop_cams()
            return
        try:
            currentImg = self.basler_recorder.single_view_queue.get_nowait()
            # self.log.debug(f"Nr elements in q {self.basler_recorder.single_view_queue.qsize()}")
            self.statusbar.showMessage(f"In Q for {self.basler_recorder.current_cam_name} :{self.basler_recorder.single_view_queue.qsize()}")
        except Empty:  # if queue is empty just return
            return
        # self.ViewWidget.updateView(currentImg)
        self.single_camviewer.updateView(currentImg)

    def show_multiple_cam(self):
        self.stop_event = Event()
        self.basler_recorder.fps = self.FrameRateSpin.value()
        self.number_cams = self.basler_recorder.cam_array.GetSize()
        use_hw_trigger = self.HWTrig_checkBox.isChecked()
        self.basler_recorder.run_multi_cam_show(self.stop_event, use_hw_trigger)

        self.multi_view_timer = QTimer()
        self.multi_view_timer.timeout.connect(self.update_multi_view)
        self.multi_view_timer.start(int(1000 // (self.FrameRateSpin.value() * 1.2)))

        self.STOPButton.setEnabled(True)
        self.RUNButton.setEnabled(False)
        self.RECButton.setEnabled(False)
        self.ShowSingleCamButton.setEnabled(False)
        self.FrameRateSpin.setEnabled(False)  # or implement on the go change of the framerate...
        self.Rec_status.setPixmap(QtGui.QIcon("GUI/icons/VideoCamera.svg").pixmap(64))
        # change the pixmap color to green
        self.Rec_status.setStyleSheet("background-color: rgb(0, 255, 0);")
        for color_mode in self.CameraSettings.color_mode_list:
            color_mode.setEnabled(False)

        self.rec_start_time = time.monotonic()
        # create a time that executes the trigger after 500 ms delay to make sure cameras are ready
        if self.trigger and use_hw_trigger:
            self.trigger.fps = self.FrameRateSpin.value()
            self.trigger_timer = QTimer()
            self.trigger_timer.setSingleShot(True)
            self.trigger_timer.timeout.connect(self.trigger.start_trigger)
            self.trigger_timer.start(500)

    def update_multi_view(self):
        # call this from a thread ? or maybe not
        if self.basler_recorder.error_event.is_set():  # if an error occured
            self.log.error('Error in Basler recorder')
            self.stop_cams()
            if self.socket_comm:
                self.socket_comm.send_json_message(SocketMessage.respond_recording_fail)
            return

        self.timer_update_counter += 1
        if self.timer_update_counter >= 20:
            self.update_rec_timer()  # dont call this too often ?
            self.timer_update_counter = 0
        show = not self.DisableViz_checkBox.isChecked()
        for c_id in range(self.number_cams):
            latest = None
            while True:
                try:
                    latest = self.basler_recorder.multi_view_queue[c_id].get_nowait()
                except Empty:
                    break
            if show and latest is not None:
                self.MultiViewWidget.cam_viewers[c_id].updateView(latest)
        # self.ViewWidget.updateView(currentImg)
        # self.ViewWidget.updateView(stitched_image)

        if not self.basler_recorder.is_recording and not self.basler_recorder.is_viewing:
            self.log.error('Basler recording stopped internally')
            self.socket_comm.send_json_message(SocketMessage.respond_recording_fail)

    def update_rec_timer(self):
        current_run_time = time.monotonic() - self.rec_start_time
        if current_run_time >= 60:
            self.recording_duration_label.setText(f"{(current_run_time // 60):.0f}m:{(current_run_time % 60):2.0f}s")
        else:
            self.recording_duration_label.setText(f"{current_run_time:.0f}s")

    def _update_rec_status_bar(self):
        qs = "  ".join(f"Q{i}: {q.qsize()}"
                       for i, q in enumerate(self.basler_recorder.multi_view_queue))
        writer_state = (self.basler_recorder.video_writer_list[0].get_state()
                        if self.basler_recorder.video_writer_list else "not recording")
        self.statusbar.showMessage(f"{qs}  |  {writer_state}")

    #### SETTINGS ###
    def save_settings(self):
        """
        Save current camera settings to a json file
        """
        if not self.basler_recorder.cams_connected:
            self.log.info('Not connected to cameras cant save settings')

            QMessageBox.information(self,
                                    "Info",
                                    "Not connected to cameras, cant save settings",
                                    buttons=QMessageBox.StandardButton.Ok)
            return

        # get active camera settings.. save those to json with cam name
        cam_lib = {}
        global_crf = self.crf_spinBox.value()
        for cam in self.basler_recorder.cam_array:
            cam_settings = self.basler_recorder.get_cam_settings(cam)
            cam_lib.update(**cam_settings)
            cam_name = cam.DeviceInfo.GetUserDefinedName()
            cam_lib[cam_name]['crf'] = self.basler_recorder.per_cam_crf.get(cam_name, global_crf)

        cam_lib.update(**{'save_path': self.basler_recorder.save_path, 'fps': self.FrameRateSpin.value(),
                          "HW_trigg": self.HWTrig_checkBox.isChecked(), 'codec': self.Codec_comboBox.currentText(),
                          "crf": global_crf})

        # open file dialog for where to save
        settings_file = QFileDialog.getSaveFileName(self, 'Save settings file', "",
                                                    "Settings files name (*.settings.json)")
        if settings_file[0]:
            filename = settings_file[0]
            if len(filename.split(".")) < 2:
                filename += '.settings.json'
            with open(filename, 'w') as fi:
                json.dump(cam_lib, fi, indent=4)

    def load_settings(self, file: (str, Path, None) = None):
        """
        Load camera settings from a json file
        """
        if not self.basler_recorder.cams_connected:
            self.log.warning('Not connected to cameras cant load settings')

            QMessageBox.information(self,
                                    "Info",
                                    "Not connected to cameras, cant load settings",
                                    buttons=QMessageBox.StandardButton.Ok)
            return

        if file is None or not file:
            settings_file = QFileDialog.getOpenFileName(self, 'Open settings file', "",
                                                        "Settings files (*.settings.json)")
            if settings_file[0]:
                file = settings_file[0]
            else:
                return

        with open(file, 'r') as fi:
            cam_lib = json.load(fi)

        for c_id, cam in enumerate(self.basler_recorder.cam_array):
            cam_name = cam.DeviceInfo.GetUserDefinedName()
            try:
                settings = cam_lib[cam_name]
            except KeyError:
                self.log.info(f'No settings found for cam: {cam_name} '
                              f'with SN: {cam.DeviceInfo.GetSerialNumber()}')
                continue
            self.basler_recorder.set_cam_settings(cam, settings)
            if 'crf' in settings:
                self.basler_recorder.per_cam_crf[cam_name] = settings['crf']
            self.CameraSettings.exposure_spin_list[c_id].blockSignals(True)
            self.CameraSettings.gain_spin_list[c_id].blockSignals(True)
            self.CameraSettings.color_mode_list[c_id].blockSignals(True)
            self.CameraSettings.exposure_spin_list[c_id].setValue(settings['exp_time'])
            self.CameraSettings.gain_spin_list[c_id].setValue(settings['gain'])
            self.CameraSettings.color_mode_list[c_id].setCurrentText(settings['color_mode'])
            self.CameraSettings.exposure_spin_list[c_id].blockSignals(False)
            self.CameraSettings.gain_spin_list[c_id].blockSignals(False)
            self.CameraSettings.color_mode_list[c_id].blockSignals(False)

        if 'HW_trigg' in cam_lib:
            self.HWTrig_checkBox.setChecked(cam_lib['HW_trigg'])
        if 'crf' in cam_lib:
            self.crf_spinBox.setValue(cam_lib['crf'])
        if 'codec' in cam_lib:
            self.Codec_comboBox.setCurrentText(cam_lib['codec'])
        if 'save_path' in cam_lib:
            self.set_save_path(cam_lib['save_path'])

    def set_save_path(self, save_path: (str, Path, None) = None):
        """
        Set the path where to save the recordings
        """
        if save_path is None or not save_path:
            save_path = QFileDialog.getExistingDirectory(self, "Select Directory where videos should be saved")
        if save_path:
            self.basler_recorder.save_path = save_path
            self.log.debug(f'Save path set to {save_path}')
            self.SavePath_label.setText(f'Save path:\n{save_path}')

    ## IMAGE CONTROL ####
    def get_current_tab(self) -> int:
        """Returns the ID of currently open tab"""
        return self.CameraSettings.toolbox.currentIndex()

    # those functions are now blocking ? maybe make sure they r not ? create threads for actual adjustments ?
    def auto_expose(self):
        """Runs autoexposure routine for given/all camera"""
        if self.All_cams_checkBox.isChecked():
            for current_camid in range(len(self.basler_recorder.cam_array)):
                final_exp = self.basler_recorder.run_auto_exposure(current_camid)
                # todo block triggerign of setting values !
                self.CameraSettings.exposure_spin_list[current_camid].setValue(final_exp)
        else:
            current_camid = self.get_current_tab()
            final_exp = self.basler_recorder.run_auto_exposure(current_camid)
            self.CameraSettings.exposure_spin_list[current_camid].setValue(final_exp)

    def auto_gain(self):
        """Runs autogain routine for given/all camera"""
        if self.All_cams_checkBox.isChecked():
            for current_camid in range(len(self.basler_recorder.cam_array)):
                final_gain = self.basler_recorder.run_auto_gain(current_camid)
                self.CameraSettings.gain_spin_list[current_camid].setValue(final_gain)
        else:
            current_camid = self.get_current_tab()
            final_gain = self.basler_recorder.run_auto_gain(current_camid)
            self.CameraSettings.gain_spin_list[current_camid].setValue(final_gain)

    def white_balance(self):
        """Runs auto white balance routine for given/all camera"""
        if self.All_cams_checkBox.isChecked():
            for current_camid in range(len(self.basler_recorder.cam_array)):
                self.basler_recorder.run_white_balance(current_camid)
        else:
            current_camid = self.get_current_tab()
            self.basler_recorder.run_white_balance(current_camid)

    def set_gain_exposure(self):
        """set the gain and exposure time for the current camera"""
        current_camid = self.get_current_tab()
        exp_time = self.CameraSettings.exposure_spin_list[current_camid].value()
        gain = self.CameraSettings.gain_spin_list[current_camid].value()
        self.basler_recorder.set_gain_exposure(current_camid, gain, exp_time)

    def set_color_mode(self, color_mode: str):
        """set the colormode for the current camera, this is poorly used by ImageViewer.py"""
        current_camid = self.get_current_tab()
        self.basler_recorder.set_color_mode(current_camid, color_mode)
        # exp_time = self.CameraSettings.exposure_spin_list[current_camid]

    def flip_x(self):
        """
        Flip image on x axis
        """
        current_camid = self.get_current_tab()
        self.basler_recorder.flip_image_x(current_camid)

    def flip_y(self):
        """
        Flip image on y axis
        """
        current_camid = self.get_current_tab()
        self.basler_recorder.flip_image_y(current_camid)

    #### APP MAINTANCE #######
    def ConnectSignals(self):
        self.ScanDevButton.clicked.connect(self.scan_cams)
        self.ConnectButton.clicked.connect(self.connect_to_cams)
        self.RUNButton.clicked.connect(self.show_multiple_cam)
        self.RECButton.clicked.connect(self.start_recording)
        self.STOPButton.clicked.connect(self.stop_cams)

        self.SettingsSaveButton.clicked.connect(self.save_settings)
        self.SettingsLoadButton.clicked.connect(self.load_settings)

        self.AutoExposeButton.clicked.connect(self.auto_expose)
        self.AutoGainButton.clicked.connect(self.auto_gain)
        self.WhiteBalanceButton.clicked.connect(self.white_balance)
        self.FlipXButton.clicked.connect(self.flip_x)
        self.FlipYButton.clicked.connect(self.flip_y)

        self.ShowSingleCamButton.clicked.connect(self.show_single_cam)

        self.Save_pathButton.clicked.connect(self.set_save_path)
        if ENABLE_REMOTE:
            self.RemoteModeButton.clicked.connect(self.remote_mode)
        self.REC_calib_Button.clicked.connect(self.start_recording_calib)

        if USE_ARDUINO_TRIGGER:
            self.ConnectB.clicked.connect(self.connect_to_pico)
            self.DisConnectB.clicked.connect(self.disconnect_from_pico)
            self.PingB.clicked.connect(self.pingPython)  #enable the ping function

    def remote_mode(self):
        if not self.is_remote_ctr: #if we are not in remote mode
            self.socket_comm.threaded_accept_connection()
            remote_dialog = RemoteConnDialog(self.socket_comm, self)
            remote_dialog.exec()

            if not self.socket_comm.connected:
                self.log.debug('Aborted remote connection')
            else:
                self.log.debug('Connected')
                self.enter_remote_mode()
        else:
            # self.abort_remoteconnection()
            self.exit_remote_mode()

    def enter_remote_mode(self):
        self.Client_label.setText(f"Connected to Client:\n{self.socket_comm.addr}")
        self.RemoteModeButton.setText("EXIT\nREMOTE-mode")
        self.RemoteModeButton.setIcon(QtGui.QIcon("GUI/icons/SignalSlash.svg"))
        self.RUNButton.setEnabled(False)
        self.RECButton.setEnabled(False)
        self.ShowSingleCamButton.setEnabled(False)
        self.SettingsSaveButton.setEnabled(False)
        self.SettingsLoadButton.setEnabled(False)
        self.Save_pathButton.setEnabled(False)
        self.CameraSettings.setEnabled(False)
        self.Codec_comboBox.setEnabled(False)
        self.crf_spinBox.setEnabled(False)
        self.HWTrig_checkBox.setEnabled(False)
        self.AutoExposeButton.setEnabled(False)
        self.AutoGainButton.setEnabled(False)
        self.WhiteBalanceButton.setEnabled(False)
        self.FlipXButton.setEnabled(False)
        self.FlipYButton.setEnabled(False)
        self.FrameRateSpin.setEnabled(False)
        self.SessionIDlineEdit.setEnabled(False)

        self.remote_message_timer = QTimer()
        self.remote_message_timer.timeout.connect(self.check_and_parse_messages)
        self.remote_message_timer.start(500)
        self.is_remote_ctr = True
        self.socket_comm.send_json_message(SocketMessage.status_ready)

    def exit_remote_mode(self):
        self.is_remote_ctr = False
        self.socket_comm.stop_waiting_for_connection() #stop waiting for background thread conn
        self.socket_comm.close_socket()
        self.Client_label.setText("disconnected")
        self.RemoteModeButton.setText("Enable\nREMOTE-mode")
        self.RemoteModeButton.setIcon(QtGui.QIcon("GUI/icons/Signal.svg"))
        if self.remote_message_timer:
            self.remote_message_timer.stop()
            self.remote_message_timer = None


        # enable all buttons
        self.RUNButton.setEnabled(True)
        self.RECButton.setEnabled(True)
        self.ShowSingleCamButton.setEnabled(True)
        self.SettingsSaveButton.setEnabled(True)
        self.SettingsLoadButton.setEnabled(True)
        self.FrameRateSpin.setEnabled(True)
        self.Save_pathButton.setEnabled(True)
        self.CameraSettings.setEnabled(True)
        self.Codec_comboBox.setEnabled(True)
        self.crf_spinBox.setEnabled(True)
        self.HWTrig_checkBox.setEnabled(True)
        self.AutoExposeButton.setEnabled(True)
        self.AutoGainButton.setEnabled(True)
        self.WhiteBalanceButton.setEnabled(True)
        self.FlipXButton.setEnabled(True)
        self.FlipYButton.setEnabled(True)
        self.SessionIDlineEdit.setEnabled(True)
        self.SessionIDlineEdit.setText("")

    def check_and_parse_messages(self):
        try:
            message = self.socket_comm.read_json_message_fast_linebreak()
        except Exception as e:
            self.log.warning(f"Socket read error in check_and_parse_messages: {e}")
            return
        if message:
            # parse message
            if message['type'] == MessageType.start_video_rec.value \
                    or message['type'] == MessageType.start_video_view.value \
                    or message['type'] == MessageType.start_video_calibrec.value:
                if self.basler_recorder.is_recording or self.basler_recorder.is_viewing:
                    # got record but we already are !
                    self.socket_comm.send_json_message(SocketMessage.status_error)
                    self.log.info("got message to start, but something is already running!")
                    return

                # combined for rec types
                try:
                    if message["setting_file"]:
                        self.load_settings(message["setting_file"])
                        self.log.debug(f"loaded settings from {message['setting_file']}")
                except (FileNotFoundError, KeyError):
                    self.log.error("passed settings file not found")

                #overwrite default save path from master script if it exists
                try:
                    if message["save_path"]:
                        self.log.debug(f"Setting save path to {message['save_path']}")
                        self.set_save_path(message["save_path"])
                except KeyError:
                    pass

                self.session_id = message["session_id"]
                self.SessionIDlineEdit.setText(self.session_id)
                try:
                    if message["frame_rate"]:
                        self.FrameRateSpin.setValue(message["frame_rate"])
                except KeyError:
                    pass
                self.remote_message_timer.setInterval(500)

                if message['type'] == MessageType.start_video_rec.value:
                    self.log.info("got message to start recording")
                    self.start_recording()
                    self.socket_comm.send_json_message(SocketMessage.respond_recording)

                elif message['type'] == MessageType.start_video_view.value:
                    self.log.info("got message to start viewing")
                    self.show_multiple_cam()
                    self.socket_comm.send_json_message(SocketMessage.respond_viewing)

                elif message['type'] == MessageType.start_video_calibrec.value:
                    self.log.info("got message to start calibration_rec")
                    self.start_recording_calib()
                    self.socket_comm.send_json_message(SocketMessage.respond_calib)

            elif message['type'] == MessageType.stop_video.value:
                self.log.info("got message to stop")
                self.stop_cams()
                self.remote_message_timer.setInterval(500)
                self.socket_comm.send_json_message(SocketMessage.respond_stop)
                self.socket_comm.close_client_socket() #only close client conn
                if self.is_remote_ctr: #only if remote ctr is enabled
                    self.socket_comm.threaded_accept_connection() #sets to listen for new conn

            elif message['type'] == MessageType.poll_status.value:
                if self.basler_recorder.is_recording:
                    self.socket_comm.send_json_message(SocketMessage.status_recording)
                elif self.basler_recorder.is_viewing:
                    self.socket_comm.send_json_message(SocketMessage.status_viewing)
                elif self.is_remote_ctr:
                    self.socket_comm.send_json_message(SocketMessage.status_ready)
                else:
                    self.socket_comm.send_json_message(SocketMessage.status_error)

            elif message['type'] == MessageType.disconnected.value:
                self.log.info("got message that client disconnected")
                if self.is_remote_ctr:
                    self.socket_comm.threaded_accept_connection()
                else:
                    self.exit_remote_mode()

            elif message['type'] == MessageType.copy_files.value:
                self.log.debug('got message to copy files')
                self.session_path = message['session_path']
                if self.session_path:
                    Thread(target=self.copy_recorded_file, daemon=True).start()

            elif message['type'] == MessageType.purge_files.value:
                self.log.debug('got message to purge files')
                self.purge_recorded_file()

    def purge_recorded_file(self):
        for videowriter in self.basler_recorder.video_writer_list:
            if videowriter.stopped:
                mp4_path = Path(videowriter.video_path)
                txt_path = mp4_path.with_suffix('.txt')
                self.log.info(f"Deleting file {mp4_path}")
                mp4_path.unlink()
                if txt_path.exists():
                    self.log.info(f"Deleting file {txt_path}")
                    txt_path.unlink()
            else:
                self.log.info(f"Cant delete file {videowriter.video_path} as recorder hasnt finished yet")

    def copy_recorded_file(self):
        if not self.basler_recorder.is_recording and not self.files_copied:
            for videowriter in self.basler_recorder.video_writer_list:
                if videowriter.stopped:
                    self.log.info(f"Copying file {videowriter.video_path} to {Path(self.session_path) / VIDEO_FOLDER}")

                    try:
                        mp4_src = Path(videowriter.video_path)
                        txt_src = mp4_src.with_suffix('.txt')
                        if 'MusterMaus' in self.session_id:
                            dst_dir = Path(self.session_path)
                        else:
                            dst_dir = Path(self.session_path) / VIDEO_FOLDER
                            dst_dir.mkdir(exist_ok=True)
                        shutil.copyfile(mp4_src, dst_dir / mp4_src.name)
                        if txt_src.exists():
                            shutil.copyfile(txt_src, dst_dir / txt_src.name)
                    except (FileNotFoundError, IOError) as e:
                        self.socket_comm.send_json_message(SocketMessage.respond_copy_fail)
                        self.log.error(f"Error copying file {e}")
                        return
            self.files_copied = True
            self.log.info(f"Finished copying files to {Path(self.session_path) / VIDEO_FOLDER}")
            self.socket_comm.send_json_message(SocketMessage.respond_copy)

    def app_is_exiting(self):
        """Routine to be run when the app is exiting, cleanup and release of resources"""
        # check if recording is running stop if does.
        self.stop_cams()  # stop any grabbing still ongoing
        if self.socket_comm:
            self.socket_comm.close_socket()
        self.basler_recorder.disconnect_cams()  # close and release cameras

    def closeEvent(self, event):
        """
        Overriden close event to make sure that all cameras are closed and all threads are stopped
        """
        self.log.info("Received window close event.")

        # If recording is still running, ask if user wants to abort
        if self.basler_recorder.is_recording or self.is_remote_ctr:
            message_text = "Recording still running. Abort ?" if self.basler_recorder.is_recording \
                else "Remote mode is active. Abort ?"

            message = QMessageBox.information(self,
                                              "Really quit?",
                                              message_text,
                                              buttons=QMessageBox.StandardButton.No | QMessageBox.StandardButton.Yes)
            if message == QMessageBox.StandardButton.No or message == QMessageBox.StandardButton.Abort:
                event.ignore()
                return
            elif message == QMessageBox.StandardButton.Yes:
                self.log.info('Exiting')
        self.app_is_exiting()
        super(BASLER_GUI, self).closeEvent(event)


def start_gui():
    app = QApplication([])
    win = BASLER_GUI()
    win.show()
    app.exec()


if __name__ == '__main__':
    logging.info('Starting via __main__')
    sys.exit(start_gui())
