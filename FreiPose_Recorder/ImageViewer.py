import logging
from pathlib import Path
import pyqtgraph as pg
from pyqtgraph import ImageView, RawImageWidget, GraphicsView, ImageItem, GraphicsWidget, PlotWidget
from datetime import datetime
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QDialog, QSizePolicy, \
    QGridLayout, QToolBox,  QDoubleSpinBox, QComboBox, QLabel
from PyQt6 import uic, QtCore, QtGui, QtWidgets
import numpy as np

import cv2
import time


class MultiCameraViewer(QWidget):
    """
    A widget that displays the images from multiple cameras.
    """
    def __init__(self, parent=None, num_cameras=4):
        super().__init__(parent)
        self.grid = None
        self._num_cameras = num_cameras
        self.cam_viewers = []
        self.parent = parent
        self.init_ui()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    @property
    def num_cameras(self):
        return self._num_cameras

    @num_cameras.setter
    def num_cameras(self, value):
        if 0 < value <= 9:
            self._num_cameras = value

        else:
            self._num_cameras = 9
        self.change_ui()

    def init_ui(self):
        # create a grid layout to hold the camera views
        self.grid = QGridLayout()

        # self.grid.setSpacing(-50)
        # self.grid.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self.grid)

        # create a RawImageWidget for each camera and add it to the layout
        if self.num_cameras <= 4:
            step = 2
        else:
            step = 3
        for i in range(self.num_cameras):
            widget = ImageView_camera(self.parent)
            self.cam_viewers.append(widget)
            self.grid.addWidget(widget, i // step, i % step)
        self._equalize_cells(step)

        self.show()
        self.grid.setSpacing(0)

    def change_ui(self):
        for view in self.cam_viewers:
            self.grid.removeWidget(view)
        self.cam_viewers = []
        if self.num_cameras <= 4:
            step = 2
        else:
            step = 3
        for i in range(self.num_cameras):
            widget = ImageView_camera(self.parent)
            self.cam_viewers.append(widget)
            self.grid.addWidget(widget, i // step, i % step)
        self._equalize_cells(step)

    def _equalize_cells(self, step):
        """Give every grid row/column the same share of space, regardless of image size."""
        rows = (self.num_cameras + step - 1) // step
        for r in range(3):
            self.grid.setRowStretch(r, 1 if r < rows else 0)
        for c in range(3):
            self.grid.setColumnStretch(c, 1 if c < step else 0)


class ImageView_camera(QWidget):
    def __init__(self, parent=None):
        super(ImageView_camera, self).__init__(parent)

        # Create a layout for the image widget
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        #layout.setSpacing(0)
        # Create a RawImageWidget
        self.image_view = pg.RawImageWidget(scaled=True)
        # Ignored: the image's own size must not influence how much grid space this view gets
        self.image_view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image_view.setMinimumSize(1, 1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

        # Add the RawImageWidget to the layout
        layout.addWidget(self.image_view)

        self.image_view.setImage(np.random.randint(0, 255, (128, 128), np.uint8))

    def updateView(self, image):
        """
        Set the image to be displayed in the RawImageWidget.
        image: numpy array containing the image data
        """
        # rotate img such that if it 2 dimentional it s transposed if 3 dimentional only first 2 axis are transposed
        try:
            if len(image.shape) == 3:
                self.image_view.setImage(image.transpose(1, 0, 2))
            else:
                self.image_view.setImage(image.T)
        except ValueError:
            print("Image could not be displayed. this format is not implemented")


class SingleCamViewer(QDialog):
    def __init__(self, parent, cam_name):
        super(SingleCamViewer, self).__init__(parent)
        self.path2file = Path(__file__)
        uic.loadUi(self.path2file.parent / 'GUI' / 'SingleCameraView.ui', self)
        self.setWindowTitle(f"CamViewer {cam_name}")
        self.log = logging.getLogger('CamViewer')
        self.log.setLevel(logging.DEBUG)
        self.parent = parent
        self.ConnectSignals()
        self.is_showing = True

    def ConnectSignals(self):
        self.STOPButton.clicked.connect(self.stop_viewing)

        self.AutoExposeButton.clicked.connect(self.auto_expose)
        self.AutoGainButton.clicked.connect(self.auto_gain)
        self.WhiteBalanceButton.clicked.connect(self.white_balance)
        self.FlipXButton.clicked.connect(self.flip_x)
        self.FlipYButton.clicked.connect(self.flip_y)

    def updateView(self, img):
        self.CamViewer.updateView(img)

    def stop_viewing(self):
        self.log.debug('Indicating to main to stop grabbing')
        self.is_showing = False
        self.parent.stop_cams()

    def auto_expose(self):
        self.parent.auto_expose()

    def auto_gain(self):
        self.parent.auto_gain()

    def white_balance(self):
        self.parent.white_balance()

    def flip_x(self):
        self.parent.flip_x()

    def flip_y(self):
        self.parent.flip_y()

    def app_is_exiting(self):
        """routine to call for stop if window is closed"""
        self.stop_viewing()

    def closeEvent(self, event):
        if self.is_showing:
            self.log.debug("Received window close event.")
            self.app_is_exiting()
            super(SingleCamViewer, self).closeEvent(event)
        else:
            self.log.debug("Closed by parent")

class SingleCameraSettings(QWidget):
    def __init__(self, parent=None, name='Camera'):
        super(SingleCameraSettings, self).__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)

        font = QtGui.QFont()
        font.setPointSize(9)

        self.exp_label = QLabel(self)
        self.exp_label.setText("Exposure Time")
        self.ExposureTime_spin = QDoubleSpinBox(self)
        self.ExposureTime_spin.setSuffix(" us")
        self.ExposureTime_spin.setMinimum(0.5)
        self.ExposureTime_spin.setMaximum(1000000.0)
        self.ExposureTime_spin.setSingleStep(0.5)
        self.ExposureTime_spin.setProperty("value", 5.0)
        self.layout.addWidget(self.exp_label)
        self.layout.addWidget(self.ExposureTime_spin)
        hbox = QHBoxLayout()
        hbox.addWidget(self.exp_label)
        hbox.addWidget(self.ExposureTime_spin)
        self.layout.addLayout(hbox)


        self.gain_label = QLabel(self)
        self.gain_label.setText("Gain")
        self.Gain_spin = QDoubleSpinBox(self)
        self.Gain_spin.setMinimum(0.0)
        self.Gain_spin.setMaximum(48.0)
        self.Gain_spin.setSingleStep(0.1)
        self.Gain_spin.setProperty("value", 0.0)
        hbox = QHBoxLayout()
        hbox.addWidget(self.gain_label)
        hbox.addWidget(self.Gain_spin)
        self.layout.addLayout(hbox)
        #self.layout.addWidget(self.Gain_spin)

        self.colorlabel = QLabel(self)
        self.colorlabel.setText("Color mode")
        self.ColorMode_comboBox = QComboBox(self)
        self.layout.addWidget(self.colorlabel)
        self.layout.addWidget(self.ColorMode_comboBox)

        self.setLayout(self.layout)
        self.setFont(font)
        self.show()
    def set_colormodes(self, colormodes:list):
        self.ColorMode_comboBox.clear()
        self.ColorMode_comboBox.addItems(colormodes)

# create a class to dynamically create camera tabs according to number of cameras
class CameraSettingsTab(QWidget):
    def __init__(self, parent=None, nr_cams=4):
        super(CameraSettingsTab, self).__init__(parent)
        self.log = logging.getLogger('CameraTab')
        self.log.setLevel(logging.DEBUG)
        self.parent = parent
        self._num_cameras = nr_cams
        self.cam_settings = []
        #self.log.debug('CameraTab created')
        self.gain_spin_list = []
        self.exposure_spin_list = []
        self.color_mode_list = []

        self.init_ui()
        self.ConnectSignals()

    @property
    def num_cameras(self):
        return self._num_cameras

    @num_cameras.setter
    def num_cameras(self, value):
        if 0 < value <= 9:
            self._num_cameras = value

        else:
            self._num_cameras = 9
        self.change_ui()

    def init_ui(self):
        font = QtGui.QFont()
        font.setPointSize(9)
        self.layout= QVBoxLayout(self)
        self.setLayout(self.layout)
        self.toolbox = QToolBox()

        for i in range(self.num_cameras):
            cam_sett = SingleCameraSettings(self)
            self.toolbox.insertItem(i, cam_sett, f'Camera {i}')
            self.gain_spin_list.append(cam_sett.Gain_spin)
            self.exposure_spin_list.append(cam_sett.ExposureTime_spin)
            self.color_mode_list.append(cam_sett.ColorMode_comboBox)
        self.layout.addWidget(self.toolbox)
        self.setFont(font)
        self.show()

    def change_ui(self):
        self.layout.removeWidget(self.toolbox)
        self.gain_spin_list = []
        self.exposure_spin_list = []
        self.color_mode_list = []

        self.toolbox = QToolBox()
        for i in range(self.num_cameras):
            cam_sett = SingleCameraSettings(self)
            self.toolbox.insertItem(i, cam_sett, f'Camera {i}')
            self.gain_spin_list.append(cam_sett.Gain_spin)
            self.exposure_spin_list.append(cam_sett.ExposureTime_spin)
            self.color_mode_list.append(cam_sett.ColorMode_comboBox)
        self.layout.addWidget(self.toolbox)
        self.ConnectSignals()  # reconnect with new widgets

    def parent_gain_exposure(self):
        self.parent.parent().set_gain_exposure()  #because of the promoted parent widget

    def parent_color_mode(self, color_mode: str):
        """
        Set the color mode of the camera, as callback to changes in UI
        """
        self.parent.parent().set_color_mode(color_mode)

    def ConnectSignals(self):
        for spinbox in self.exposure_spin_list:
            spinbox.valueChanged.connect(self.parent_gain_exposure)
        for spinbox in self.gain_spin_list:
            spinbox.valueChanged.connect(self.parent_gain_exposure)
        for spinbox in self.color_mode_list:
            spinbox.currentTextChanged.connect(self.parent_color_mode)


class RemoteConnDialog(QtWidgets.QDialog):
    """
    Dialog to wait for remote connection, with abort button
    """
    def __init__(self, socket_comm, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.socket_comm = socket_comm
        self.setWindowTitle('Remote Connection')
        self.abort_button = QtWidgets.QPushButton("Abort")
        self.abort_button.clicked.connect(self.stopwaiting)
        self.abort_button.setIcon(QtGui.QIcon("GUI/icons/HandRaised.svg"))
        self.label = QtWidgets.QLabel("waiting for remote connection...")
        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(self.abort_button)
        self.setLayout(layout)

        self.connectio_time = QtCore.QTimer()
        self.connectio_time.timeout.connect(self.check_connection)
        self.connectio_time.start(500)
        self.aborted = False

    def check_connection(self):
        """
        Check if connection is established, if so close dialog, called regularly by timer
        """
        if self.socket_comm.connected:
            self.close()

    def stopwaiting(self):
        """
        Stop waiting for connection, called by abort button
        """
        self.socket_comm.stop_waiting_for_connection()
        self.aborted = True
        self.close()

    def closeEvent(self, event):
        # If the user closes the dialog, kill the process
        self.stopwaiting()
        self.aborted = True
        event.accept()