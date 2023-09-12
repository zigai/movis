from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Hashable, Iterator, Sequence
from weakref import WeakValueDictionary

import cv2
import imageio
import soundfile as sf
import numpy as np
from diskcache import Cache
from tqdm import tqdm

from ..attribute import Attribute
from ..effect import Effect
from ..enum import BlendingMode, CacheType, Direction
from ..imgproc import alpha_composite
from ..transform import Transform, TransformValue
from .protocol import Layer, AudioLayer, AUDIO_SAMPLING_RATE


class Composition:
    """A base layer that integrates multiple layers into one video.

    Users create a composition by specifying both time and resolution. Next, multiple layers can be added to
    the target composition through ``Composition.add_layer()``. During this process, additional information such as
    the layer's name, start time, position, opacity, and drawing mode can be specified.
    Finally, the composition integrates the layers in the order they were added to create a single video.

    Another composition can also be added as a layer within a composition.
    By nesting compositions in this way, more complex motions can be created.

    Args:
        size:
            A tuple representing the size of the composition in the form of ``(width, height)``.
        duration:
            The duration along the time axis for the composition.
    """

    def __init__(
        self, size: tuple[int, int] = (1920, 1080), duration: float = 1.0
    ) -> None:
        self._layers: list[LayerItem] = []
        self._name_to_layer: WeakValueDictionary[str, LayerItem] = WeakValueDictionary()
        self._duration = duration
        self._cache: Cache = Cache(size_limit=1024 * 1024 * 1024)
        self._preview_level: int = 1
        self._size = size

    @property
    def size(self) -> tuple[int, int]:
        """The size of the composition in the form of ``(width, height)``."""
        return self._size

    @size.setter
    def size(self, size: tuple[int, int]) -> None:
        assert len(size) == 2
        assert size[0] > 0 and size[1] > 0
        self._size = (int(size[0]), int(size[1]))

    @property
    def duration(self) -> float:
        """The duration of the composition."""
        return self._duration

    @duration.setter
    def duration(self, duration: float) -> None:
        assert duration > 0
        self._duration = float(duration)

    @property
    def preview_level(self) -> int:
        """The resolution of the rendering of the composition.
        For example, if ``preview_level=2`` is set,
        the composition's resolution is ``(W / 2, H / 2)``."""
        return self._preview_level

    @preview_level.setter
    def preview_level(self, level: int) -> None:
        assert level > 0, "preview_level must be greater than 0"
        self._preview_level = int(level)

    @contextmanager
    def preview(self, level: int = 2) -> Iterator[None]:
        """Context manager method to temporarily change the ``preview_level`` using the `with` syntax.

        For example, ``with self.preview(level=2):`` would change the ``preview_level`` to 2 in that scope.
        """
        assert level > 0
        original_level = self._preview_level
        self._preview_level = level
        try:
            yield
        finally:
            self._preview_level = original_level

    @property
    def layers(self) -> Sequence[LayerItem]:
        """Returns a list of ``LayerItem`` objects."""
        return self._layers

    def keys(self) -> list[str]:
        """Returns a list of layer names.

        Note that the keys are sorted in the order in which they will be rendered.

        Returns:
            A list of layer names sorted in the rendering order.
        """
        return [layer.name for layer in self._layers]

    def values(self) -> list[LayerItem]:
        """Returns a list of LayerItem objects.

        Note that the elements of the list are not the layers themselves,
        but `LayerItem` containing information of the layers.

        Returns:
            A list of ``LayerItem`` objects.
        """
        return self._layers

    def items(self) -> list[tuple[str, LayerItem]]:
        """Returns a list of tuples, each consisting of a layer name and its corresponding item.

        Returns:
            A list of tuples, where each tuple contains a layer name and its layer item.
        """
        return [(layer.name, layer) for layer in self._layers]

    def __len__(self) -> int:
        return len(self._layers)

    def __getitem__(self, key: str) -> LayerItem:
        return self._name_to_layer[key]

    def __contains__(self, key: str) -> bool:
        return key in self._name_to_layer

    def __setitem__(self, key: str, value: LayerItem | Layer) -> None:
        if isinstance(value, LayerItem):
            self._layers.append(value)
            self._name_to_layer[key] = value
        elif callable(value):
            self.add_layer(value, name=key)
        else:
            raise ValueError("value must be LayerItem or Layer (i.e., callable)")

    def __delitem__(self, key: str) -> None:
        self.pop_layer(key)

    def get_key(self, time: float) -> tuple[Hashable, ...]:
        """Returns a tuple of hashable keys representing the state for each layer at the given time."""
        layer_keys: list[Hashable] = [CacheType.COMPOSITION]
        for layer_item in self._layers:
            layer_time = time - layer_item.offset
            if layer_time < layer_item.start_time or layer_item.end_time <= layer_time:
                layer_keys.append(None)
            else:
                layer_keys.append(layer_item.get_key(layer_time))
        return tuple(layer_keys)

    def __repr__(self) -> str:
        return f"Composition(size={self.size}, duration={self.duration}, layers={self._layers!r})"

    def add_layer(
        self,
        layer: Layer,
        name: str | None = None,
        position: tuple[float, float] | None = None,
        scale: float | tuple[float, float] | np.ndarray = (1.0, 1.0),
        rotation: float = 0.0,
        opacity: float = 1.0,
        blending_mode: BlendingMode | str = BlendingMode.NORMAL,
        anchor_point: float | tuple[float, float] | np.ndarray = (0.0, 0.0),
        origin_point: Direction | str = Direction.CENTER,
        transform: Transform | None = None,
        offset: float = 0.0,
        start_time: float = 0.0,
        end_time: float | None = None,
        visible: bool = True,
    ) -> LayerItem:
        """Add a layer to the composition.

        This method appends the target layer to the composition, along with details about the layer such as
        position, scale, opacity, and rendering mode. The composition registers layers wrapped within
        a ``LayerItem`` object, which consolidates these related details.
        Users can also add the layer with a unique name to the composition.
        In this case, the LayerItem can be accessed using ``composition['layer_name']``.
        To access the layer directly, users can reference it as ``composition['layer_name'].layer``,
        or ideally, retain it in a separate variable before registering with ``add_layer()``.

        A composition can also be treated as a layer.
        This means that users can embed one composition within another using the ``add_layer()`` method.
        This allows for more intricate image compositions and animations.

        Args:
            layer:
                An instance or function of the layer to be added to the composition,
                conforming to the ``Layer`` protocol.
            name:
                The unique name for the layer within the composition.
            position:
                The position of the layer. If unspecified,
                the layer is placed at the center of the composition by default.
            scale:
                Scale ``(sx, sy)`` of the layer. Defaults to ``(1.0, 1.0)``.
            rotation:
                Clockwise rotation angle (in degrees) of the layer. Default is ``0.0``.
            opacity:
                Opacity of the layer. Default is ``1.0``.
            blending_mode:
                Rendering mode of the layer. Can be specified as an Enum from ``BlendingMode``
                or as a string. Defaults to ``BlendingMode.NORMAL``.
            anchor_point:
                Defines the origin of the layer's coordinate system.
                The origin is determined by the sum of ``origin_point`` and ``anchor_point``.
                If ``origin_point`` is ``Direction.CENTER`` and ``anchor_point`` is ``(0, 0)``,
                the origin is the center of the layer. Default is ``(0, 0)``.
            origin_point:
                Initial reference point for the layer's coordinate system.
                The final origin is determined by the sum of ``origin_point`` and ``anchor_point``.
                Defaults to ``Direction.CENTER`` (center of the layer).
            transform:
                A Transform object managing the geometric properties and rendering mode of the layer.
                If specified, the arguments for ``position``, ``scale``, ``rotation``, ``anchor_point``,
                ``origin_point``, and ``blending_mode`` in ``add_layer()`` are ignored
                in favor of the values in ``transform``.
            offset:
                The starting time of the layer. For example, if ``start_time=0.0`` and ``offset=1.0``,
                the layer will appear after 1 second in the composition.
            start_time:
                The start time of the layer. This variable is used to clip the layer in the time axis direction.
                For example, if ``start_time=1.0`` and ``offset=0.0``, this layer will appear immediately
                with one second skipped.
            end_time:
                The end time of the layer. This variable is used to clip the layer in the time axis direction.
                For example, if ``start_time=0.0``, ``end_time=1.0``, and ``offset=0.0``,
                this layer will disappear after one second. If not specified,
                the layer's duration is used for ``end_time``.
            visible:
                A flag specifying whether the layer is visible or not;
                if ``visible=False``, the layer in the composition is not rendered.

        Returns:
            A ``LayerItem`` object that wraps the layer and its corresponding information.
        """
        if name is None:
            name = f"layer_{len(self._layers)}"
        if name in self._name_to_layer:
            raise KeyError(f"Layer with name {name} already exists")
        end_time = end_time if end_time is not None else getattr(layer, "duration", 1e6)

        if position is None:
            position = self.size[0] / 2, self.size[1] / 2
        if transform is None:
            transform = Transform(
                position=position,
                scale=scale,
                rotation=rotation,
                opacity=opacity,
                anchor_point=anchor_point,
                origin_point=origin_point,
                blending_mode=blending_mode,
            )
        layer_item = LayerItem(
            layer,
            name=name,
            transform=transform,
            offset=offset,
            start_time=start_time,
            end_time=end_time,
            visible=visible,
        )
        self._layers.append(layer_item)
        self._name_to_layer[name] = layer_item
        return layer_item

    def pop_layer(self, name: str) -> LayerItem:
        """Removes a layer item from the composition and returns it.

        Args:
            name: The name of the layer to be removed.

        Returns:
            The layer item that was removed.
        """
        if name not in self._name_to_layer:
            raise KeyError(f"Layer with name {name} does not exist")
        index = next(i for i in range(len(self._layers)) if self._layers[i].name == name)
        layer_item = self._layers.pop(index)
        return layer_item

    def clear(self) -> None:
        """Removes all layers from the composition."""
        self._layers.clear()

    def __call__(self, time: float) -> np.ndarray | None:
        if time < 0.0 or self.duration <= time:
            return None

        L = self._preview_level
        current_shape = self.size[1] // L, self.size[0] // L

        key = self.get_key(time)
        if key in self._cache:
            cached_frame: np.ndarray = self._cache[key]
            if cached_frame.shape[:2] == current_shape:
                return cached_frame
            else:
                del self._cache[key]

        frame = np.zeros(current_shape + (4,), dtype=np.uint8)
        for layer_item in self._layers:
            frame = layer_item._composite(
                frame, time, preview_level=self._preview_level)
        self._cache[key] = frame
        return frame

    def get_audio(self, start_time: float = 0.0, end_time: float | None = None) -> np.ndarray | None:
        """Returns the audio of the composition as a numpy array.

        Args:
            start_time:
                The start time of the audio. This variable is used to clip the audio in the time axis direction.
            end_time:
                The end time of the audio. This variable is used to clip the audio in the time axis direction.
                If not specified, the composition's duration is used for ``end_time``.

        Returns:
            The audio of the composition as a numpy array. If no audio is found, ``None`` is returned.
        """
        assert start_time >= 0, "start_time must be nonnegative"
        assert end_time is None or start_time < end_time
        if end_time is None:
            end_time = self.duration
        target_layers = [li for li in self.layers if hasattr(li.layer, 'get_audio')]
        if len(target_layers) == 0:
            return None

        n_samples = int((end_time - start_time) * AUDIO_SAMPLING_RATE)
        audio = None
        for layer_item in target_layers:
            layer: AudioLayer = layer_item.layer  # type: ignore
            layer_time_start = max(layer_item.start_time, start_time - layer_item.offset)
            layer_time_end = min(layer_item.end_time, end_time - layer_item.offset)
            if layer_time_start >= layer_time_end:
                continue
            audio_i = layer.get_audio(layer_time_start, layer_time_end)
            if audio_i is None:
                continue
            ind_start = int((layer_time_start + layer_item.offset) * AUDIO_SAMPLING_RATE)
            if audio is None:
                audio = np.zeros((2, n_samples), dtype=np.float32)
            audio[:, ind_start: ind_start + audio_i.shape[1]] += audio_i
        return audio

    def write_video(
        self,
        dst_file: str | Path,
        start_time: float = 0.0,
        end_time: float | None = None,
        codec: str = "libx264",
        pixelformat: str = "yuv420p",
        fps: float = 30.0,
        audio: bool = False,
    ) -> None:
        """Writes the composition's contents to a video file.

        Args:
            dst_file:
                The path to the destination video file.
            start_time:
                The start time of the video. This variable is used to clip the video in the time axis direction.
            end_time:
                The end time of the video. This variable is used to clip the video in the time axis direction.
                If not specified, the composition's duration is used for ``end_time``.
            codec:
                The codec used to encode the video. Default is ``libx264``.
            pixelformat:
                The pixel format of the video. Default is ``yuv420p``.
            fps:
                The frame rate of the video. Default is ``30.0``.
            audio:
                A flag specifying whether to include audio in the video.
        """
        if end_time is None:
            end_time = self.duration
        times = np.arange(start_time, end_time, 1.0 / fps)
        if audio:
            with tempfile.NamedTemporaryFile(suffix='.wav') as audio_fp:
                audio_array = self.get_audio(start_time, end_time)
                if audio_array is None:
                    audio_path = None
                else:
                    sf.write(
                        audio_fp, audio_array.transpose(),
                        samplerate=AUDIO_SAMPLING_RATE,
                        subtype='PCM_16')
                    audio_path = audio_fp.name
                writer = imageio.get_writer(
                    uri=dst_file, fps=fps, codec=codec, pixelformat=pixelformat,
                    macro_block_size=None, ffmpeg_log_level="error",
                    audio_path=audio_path)
                for t in tqdm(times, total=len(times)):
                    frame = np.asarray(self(t))
                    writer.append_data(frame)
                writer.close()
        else:
            writer = imageio.get_writer(
                uri=dst_file, fps=fps, codec=codec, pixelformat=pixelformat,
                macro_block_size=None, ffmpeg_log_level="error")
            for t in tqdm(times, total=len(times)):
                frame = np.asarray(self(t))
                writer.append_data(frame)
            writer.close()
        self._cache.clear()

    def render_and_play(
        self,
        start_time: float = 0.0,
        end_time: float | None = None,
        fps: float = 30.0,
        preview_level: int = 2
    ) -> None:
        """Renders the composition and plays it in a Jupyter notebook.

        Args:
            start_time:
                The start time of the video. This variable is used to clip the video in the time axis direction.
            end_time:
                The end time of the video. This variable is used to clip the video in the time axis direction.
            fps:
                The frame rate of the video. Default is ``30.0``.
            preview_level:
                The resolution of the rendering of the composition.
                For example, if ``preview_level=2`` is set, the resolution of the output is ``(W / 2, H / 2)``.
                Default is ``2``.
        """
        from IPython.display import display
        from ipywidgets import Video

        if end_time is None:
            end_time = self.duration

        times = np.arange(start_time, end_time, 1.0 / fps)
        with tempfile.NamedTemporaryFile(suffix='.mp4') as fp:
            with self.preview(level=preview_level):
                filename: str = fp.name
                writer = imageio.get_writer(
                    filename, fps=fps, codec="libx264",
                    ffmpeg_params=["-preset", "veryfast"],
                    pixelformat="yuv444p", macro_block_size=None,
                    ffmpeg_log_level="error")
                for t in tqdm(times, total=len(times)):
                    frame = np.asarray(self(t))
                    writer.append_data(frame)
                writer.close()
                self._cache.clear()

                display(Video.from_file(filename, autoplay=True, loop=True))


class LayerItem:
    """A wrapper layer for managing additional info. (e.g., the name and position) of each layer in a composition.

    Usually, there is no need for the user to create this layer directly.
    However, editing additional information like the layer's position or opacity,
    or when adding animations or effects, requires editing this layer.

    ``LayerItem`` can be accessed as the return value of ``composition.add_layer()``
    or by specifying it like ``composition['layer_name']``.
    If you want to directly access the layer, refer to the ``layer_item.layer`` property.

    Args:
        layer:
            The layer to be wrapped.
        name:
            The name of the layer. The layer name must be unique within the composition.
        transform:
            An instance of ``Transform`` that includes multiple properties
            used to transform the layer within the composition.
        offset:
            The starting time of the layer. For example, if ``start_time=0.0`` and ``offset=1.0``,
            the layer will appear after 1 second in the composition.
        start_time:
            The start time of the layer. This variable is used to clip the layer in the time axis direction.
            For example, if ``start_time=1.0`` and ``offset=0.0``, this layer will appear immediately
            with one second skipped.
        end_time:
            The end time of the layer. This variable is used to clip the layer in the time axis direction.
            For example, if ``start_time=0.0``, ``end_time=1.0``, and ``offset=0.0``,
            this layer will disappear after one second.
            If not specified, the layer's duration is used for ``end_time``.
        visible:
            A flag specifying whether the layer is visible or not;
            if ``visible=False``, the layer in the composition is not rendered.
    """
    def __init__(
            self, layer: Layer, name: str = 'layer', transform: Transform | None = None,
            offset: float = 0.0, start_time: float = 0.0, end_time: float | None = None,
            visible: bool = True):
        self.layer: Layer = layer
        self.name: str = name
        self.transform: Transform = transform if transform is not None else Transform()
        self.offset: float = offset
        self.start_time: float = start_time
        self.end_time: float = end_time if end_time is not None else getattr(layer, "duration", 1e6)
        self.visible: bool = visible
        self._effects: list[Effect] = []

    @property
    def duration(self) -> float:
        """The duration of the layer item.

        Note that this value is determined by the difference between `end_time` and `start_time`,
        not by the duration of the layer.
        """
        return self.end_time - self.start_time

    def add_effect(self, effect: Effect) -> Effect:
        """Adds an effect to the layer.

        Args:
            effect:
                The effect to be added to the layer.

        Returns:
            The effect that was added.
        """
        self._effects.append(effect)
        return effect

    def remove_effect(self, effect: Effect) -> None:
        """Removes an effect from the layer.

        Args:
            effect:
                The effect to be removed from the layer.
        """
        self._effects.remove(effect)

    @property
    def effects(self) -> list[Effect]:
        """A list of effects applied to the layer."""
        return self._effects

    @property
    def anchor_point(self) -> Attribute:
        """The anchor point of the layer."""
        return self.transform.anchor_point

    @property
    def position(self) -> Attribute:
        """The position of the layer."""
        return self.transform.position

    @property
    def scale(self) -> Attribute:
        """The scale of the layer."""
        return self.transform.scale

    @property
    def rotation(self) -> Attribute:
        """The rotation of the layer."""
        return self.transform.rotation

    @property
    def opacity(self) -> Attribute:
        """The opacity of the layer."""
        return self.transform.opacity

    @property
    def origin_point(self) -> Direction:
        """The origin point of the layer."""
        return self.transform.origin_point

    @property
    def blending_mode(self) -> BlendingMode:
        """The blending mode of the layer."""
        return self.transform.blending_mode

    def get_key(self, layer_time: float) -> tuple[Hashable, Hashable, Hashable]:
        """Returns the state of the layer item at the given time.

        Args:
            layer_time:
                The time at which the layer is rendered.

        Returns:
            A tuple of hashable keys representing the state of the layer at the given time."""
        if not self.visible:
            return (None, None, None)
        transform_key = self.transform.get_current_value(layer_time)
        layer_key = self.layer.get_key(layer_time) if hasattr(self.layer, 'get_key') else layer_time

        def get_effect_key(e: Effect) -> Hashable | None:
            return e.get_key(layer_time) if hasattr(e, 'get_key') else layer_time

        effects_key = None if len(self._effects) == 0 else tuple([get_effect_key(e) for e in self._effects])
        return (transform_key, layer_key, effects_key)

    def _composite(
        self, bg_image: np.ndarray, time: float,
        parent: tuple[int, int] = (0, 0),
        preview_level: int = 1,
    ) -> np.ndarray:
        # Retrieve layer image
        layer_time = time - self.offset
        if layer_time < self.start_time or self.end_time <= layer_time:
            return bg_image
        fg_image = self(time)
        if fg_image is None:
            return bg_image
        assert isinstance(fg_image, np.ndarray), "Rendered layer image must be a numpy array"
        assert fg_image.dtype == np.uint8, "Rendered layer image must have dtype=np.uint8"
        assert fg_image.ndim == 3, "Rendered layer image must have 3 dimensions (H, W, C)"
        assert fg_image.shape[2] == 4, "Rendered layer image must have 4 channels (RGBA)"

        # Get affine matrix and transform layer image
        p = self.transform.get_current_value(layer_time)
        result = _get_fixed_affine_matrix(fg_image, p, preview_level=preview_level)
        if result is None:
            return bg_image
        affine_matrix_fixed, (W, H), (offset_x, offset_y) = result
        fg_image_transformed = cv2.warpAffine(
            fg_image, affine_matrix_fixed, dsize=(W, H),
            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

        # Composite bg_image and fg_image
        bg_image = alpha_composite(
            bg_image, fg_image_transformed,
            position=(offset_x - parent[0], offset_y - parent[1]),
            opacity=p.opacity, blending_mode=p.blending_mode)
        return bg_image

    def __call__(self, time: float) -> np.ndarray | None:
        layer_time = time - self.offset
        if not self.visible:
            return None
        frame = self.layer(layer_time)
        if frame is None:
            return None
        for effect in self._effects:
            frame = effect(frame, layer_time)
        return frame

    def __repr__(self) -> str:
        return f"LayerItem(name={self.name!r}, layer={self.layer!r}, transform={self.transform!r}, " \
            f"offset={self.offset}, visible={self.visible})"


def _get_fixed_affine_matrix(
    fg_image: np.ndarray, p: TransformValue,
    preview_level: int = 1
) -> tuple[np.ndarray, tuple[int, int], tuple[int, int]] | None:
    h, w = fg_image.shape[:2]

    T1, SR = _get_T1(p), _get_SR(p)
    T2 = _get_T2(p, (w, h), p.origin_point)
    M = T1 @ SR @ T2
    P = np.array([
        [1 / preview_level, 0, 0],
        [0, 1 / preview_level, 0],
        [0, 0, 1]], dtype=np.float64)
    affine_matrix = (P @ M)[:2]

    corners_layer = np.array([
        [0, 0, 1],
        [0, h, 1],
        [w, 0, 1],
        [w, h, 1]], dtype=np.float64)
    corners_global = corners_layer @ affine_matrix.transpose()
    min_coords = np.ceil(corners_global.min(axis=0))
    max_coords = np.floor(corners_global.max(axis=0))
    WH = (max_coords - min_coords).astype(np.int32)
    W, H = WH[0], WH[1]
    if W == 0 or H == 0:
        return None
    offset_x, offset_y = int(min_coords[0]), int(min_coords[1])

    Pf = np.array([
        [1 / preview_level, 0, - offset_x],
        [0, 1 / preview_level, - offset_y],
        [0, 0, 1]], dtype=np.float64)
    affine_matrix_fixed = (Pf @ M)[:2]
    return affine_matrix_fixed, (W, H), (offset_x, offset_y)


def _get_T1(p: TransformValue) -> np.ndarray:
    return np.array([
        [1, 0, p.position[0] + p.anchor_point[0]],
        [0, 1, p.position[1] + p.anchor_point[1]],
        [0, 0, 1]], dtype=np.float64)


def _get_SR(p: TransformValue) -> np.ndarray:
    cos_t = np.cos((2 * np.pi * p.rotation) / 360)
    sin_t = np.sin((2 * np.pi * p.rotation) / 360)
    SR = np.array([
        [p.scale[0] * cos_t, - p.scale[0] * sin_t, 0],
        [p.scale[1] * sin_t, p.scale[1] * cos_t, 0],
        [0, 0, 1]], dtype=np.float64)
    return SR


def _get_T2(p: TransformValue, size: tuple[int, int], origin_point: Direction) -> np.ndarray:
    center_point = Direction.to_vector(
        origin_point, (float(size[0]), float(size[1])))
    T2 = np.array([
        [1, 0, - p.anchor_point[0] - center_point[0]],
        [0, 1, - p.anchor_point[1] - center_point[1]],
        [0, 0, 1]], dtype=np.float64)
    return T2
