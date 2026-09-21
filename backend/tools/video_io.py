import os
import queue
import subprocess
import threading

import cv2
import numpy as np

try:
    import av
except ImportError:  # pragma: no cover - reported at runtime with installation guidance
    av = None

class FramePrefetcher:
    """
    后台线程预解码视频帧，使 I/O 与模型推理重叠。
    接口兼容 cv2.VideoCapture（read/release）。
    """

    def __init__(self, video_cap, buffer_size=10):
        self.cap = video_cap
        self._buffer = queue.Queue(maxsize=buffer_size)
        self._stopped = False
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        while not self._stopped:
            ret, frame = self.cap.read()
            self._buffer.put((ret, frame))
            if not ret:
                break

    def read(self):
        """读取下一帧，接口与 cv2.VideoCapture.read() 一致。"""
        return self._buffer.get()

    def get(self, propId):
        return self.cap.get(propId)

    def stop(self):
        """停止预读取，不释放底层 video_cap。"""
        self._stopped = True
        try:
            while not self._buffer.empty():
                self._buffer.get_nowait()
        except queue.Empty:
            pass
        self._thread.join(timeout=5)

    def release(self):
        self.stop()
        self.cap.release()


class FFmpegVideoWriter:
    """
    通过 FFmpeg 管道写入帧，使用 libx264 编码。
    接口兼容 cv2.VideoWriter（write/release）。
    """

    def __init__(self, output_path, fps, size, input_path=None):
        """Encode frames in presentation order.

        When ``input_path`` is supplied, PyAV supplies the source frame PTS for
        every write.  Rawvideo pipes cannot carry those timestamps, so the old
        FFmpeg implementation silently converted VFR sources to CFR.
        """
        if input_path is not None:
            if av is None:
                raise RuntimeError("PyAV is required to preserve input video timestamps.")
            self._init_timestamp_preserving_writer(output_path, size, input_path)
            return

        self._av_output = None
        from .ffmpeg_cli import FFmpegCLI
        w, h = size
        cmd = [
            FFmpegCLI.instance().ffmpeg_path,
            '-y',
            '-f', 'rawvideo',
            '-vcodec', 'rawvideo',
            '-s', f'{w}x{h}',
            '-pix_fmt', 'bgr24',
            '-r', str(fps),
            '-i', '-',
            '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p',
            '-crf', '18',
            '-preset', 'fast',
            '-loglevel', 'error',
            output_path
        ]
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _init_timestamp_preserving_writer(self, output_path, size, input_path):
        self._process = None
        self._input_container = av.open(input_path)
        self._input_stream = self._input_container.streams.video[0]
        self._input_frames = self._input_container.decode(self._input_stream)
        self._av_output = av.open(output_path, mode='w')
        w, h = size
        self._output_stream = self._av_output.add_stream('libx264')
        self._output_stream.width = w
        self._output_stream.height = h
        self._output_stream.pix_fmt = 'yuv420p'
        # Keep the source clock rather than deriving a new CFR clock from FPS.
        self._output_stream.time_base = self._input_stream.time_base
        self._output_stream.codec_context.time_base = self._input_stream.time_base
        self._written_frames = 0

    def write(self, frame):
        """写入一帧（numpy BGR 数组）。"""
        if frame.dtype != np.uint8:
            frame = np.clip(frame, 0, 255).astype(np.uint8)
        if self._av_output is not None:
            try:
                source_frame = next(self._input_frames)
            except StopIteration as exc:
                raise RuntimeError("VSR attempted to write more frames than were decoded from the input.") from exc
            encoded = av.VideoFrame.from_ndarray(frame, format='bgr24')
            encoded.pts = source_frame.pts
            encoded.time_base = source_frame.time_base
            for packet in self._output_stream.encode(encoded):
                self._av_output.mux(packet)
            self._written_frames += 1
            return
        try:
            self._process.stdin.write(frame.tobytes())
        except BrokenPipeError:
            pass

    def release(self):
        """关闭管道并等待编码完成。"""
        if self._av_output is not None:
            try:
                try:
                    next(self._input_frames)
                except StopIteration:
                    pass
                else:
                    raise RuntimeError("VSR wrote fewer frames than were decoded from the input.")
                for packet in self._output_stream.encode():
                    self._av_output.mux(packet)
            finally:
                self._av_output.close()
                self._input_container.close()
                self._av_output = None
            return
        try:
            self._process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self._process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            self._process.wait(timeout=5)
