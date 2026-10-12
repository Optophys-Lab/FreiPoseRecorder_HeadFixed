# import the necessary packages
import json
from threading import Thread
import time
from vidgear.gears import WriteGear
from queue import Queue

class QueueOverflow(Exception):
   """Base class for other exceptions"""
   pass


class VideoWriterFast:
    """
    Utility for faster Video writing with VideoGear.
    Basically runs writing of frames in an separate thread.
    """
    def __init__(self, video_path, fps, codec="libx264", crf=0, queue_size=512):
        self.crf = crf
        self.fps = fps
        self.codec = codec
        self.video_path = video_path

        # initialize the file video stream along with the boolean
        # used to indicate if the thread should be stopped or not
        self.stream = None  # this is initialized when we get the first frame
        self.stopped = False

        self.started = False

        self.write_speed = None

        # initialize the queue used to store frames read from
        # the video file
        self.queue_size = queue_size
        self.Q = Queue(maxsize=queue_size)
        self.frame_ts = []
        # intialize thread
        self.thread = Thread(target=self.update, args=())
        self.thread.daemon = True

    def start(self):
        """starts the thread to write frames to the video file"""
        self.started = True
        # start a thread to read frames from the file video stream
        self.thread.start()
        return self

    def update(self):
        """"""
        # keep looping infinitely
        while True:
            # if the thread indicator variable is set, stop the
            # thread
            if self.stopped:
                break

            # otherwise, ensure the there is something in the queue and the stream was initialized
            if self.Q.qsize() > 0 and self.stream is not None:
                # get the next frame from the queue
                frame = self.Q.get()

                start = time.time()
                # write to stream
                try:
                    self.stream.write(frame, rgb_mode=True)
                except ValueError as e:
                    self.stopped = True
                    print("Error writing frame to stream: {}".format(e))
                if self.write_speed is None:
                    self.write_speed = time.time() - start
                else:
                    self.write_speed = 0.85*self.write_speed + 0.15*(time.time() - start)

            else:
                time.sleep(0.001)  # Rest for 1ms, we have an empty queue

        self.stream.close()

    def feed(self, frame):
        if self.stream is None:
            quality_param = "-cq" if "nvenc" in self.codec else "-crf"
            output_params = {"-input_framerate": self.fps, "-vcodec": self.codec, quality_param: self.crf}
            #output_params = {"-input_framerate": self.fps, "-vcodec": "h264_nvenc", "-crf": 0}
            #output_params = {"-vcodec": "libx264", "-crf": 0, "-preset": "fast"}
            self.stream = WriteGear(output=self.video_path, **output_params)
            '''
            working codecs h264_nvenc, libx264, mpeg4, mpeg2video, libxvid, libx264rgb
            
            '''
        if not self.started:
            self.start()

        if not self.Q.full():
            # add the frame to the queue
            if isinstance(frame, (list, tuple)):
                self.frame_ts.append(frame[1:])
                return self.Q.put(frame[0])
            else:
                return self.Q.put(frame)
        else:
            raise QueueOverflow

    # Insufficient to have consumer use while(more()) which does
    # not take into account if the producer has reached end of
    # file stream.
    def running(self):
        return self.is_active() or not self.stopped

    def is_active(self):
        # return True if there are still frames in the queue. If stream is not stopped, try to wait a moment
        tries = 0
        while self.Q.qsize() == 0 and not self.stopped and tries < 5:
            time.sleep(0.1)
            tries += 1

        return self.Q.qsize() > 0

    def wait_to_finish(self):
        while self.is_active():
            time.sleep(0.1)

    def stop(self):
        # indicate that the thread should be stopped
        self.stopped = True
        # wait until stream resources are released (producer thread might be still grabbing frame)
        if self.started:
            self.thread.join()
        if self.frame_ts:
            with open(self.video_path.replace('.mp4', '.txt'), 'w') as f:
                json.dump(self.frame_ts, f)

    def get_state(self):
        state = f'Queue {self.Q.qsize()}/{self.queue_size};'
        if self.write_speed is None:
            state += ' Write speed nan FPS'
        else:
            state += f' Write speed {(1.0 / self.write_speed):0.1f} FPS'
        return state


if __name__ == '__main__':
    NUM_FRAMES = 30*4

    # 1. TIME FAST VERSION
    import numpy as np
    writer = VideoWriterFast('./test.avi', fps=30)
    start = time.time()
    writer.start()
    for _ in range(NUM_FRAMES):
        # time.sleep(0.1)  # time it takes to produce a frame, this is why a separate thread is faster
        writer.feed(np.random.randint(0, 255, (480, 640, 3)).astype('uint8'))
    while writer.is_active():
        print('Still active')
        time.sleep(1)
    print('Not active anymore')
    print('time passed', time.time() - start)
    writer.stop()
    print('finished')
