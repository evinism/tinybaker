from typing import Dict, Set, Union, List, Iterable, Any
from abc import abstractmethod
from .fileref import FileRef
from .workarounds import is_fileset
from .exceptions import (
    FileSetError,
    CircularFileSetError,
    BakerError,
    SeriousErrorThatYouShouldOpenAnIssueForIfYouGet,
    ConfigurationError,
    UnusedFileWarning,
)
from .context import get_default_context
from .util import get_files_in_path_dict, classproperty
from typeguard import typechecked
from .namespace_transforms import namespace_to_transform, dict_to_transform
from .tag import InputTag, OutputTag, input_files_ctx, output_files_ctx
from warnings import warn

PathDict = Dict[str, Union[str, Iterable[str]]]
FileDict = Dict[str, Union[FileRef, List[FileRef]]]
TagSet = Set[str]


@typechecked(always=True)
def _ensure_fileset_iff_fileset_tag(path_dict: PathDict):
    for tag in path_dict:
        should_be_string = not is_fileset(tag)
        value = path_dict[tag]
        if should_be_string and (type(value) != str):
            raise ConfigurationError("Tag {} expected file, got fileset".format(tag))
        if (not should_be_string) and (type(value) == str):
            raise ConfigurationError("Tag {} expected file, got fileset".format(tag))


class TransformMeta(type):
    pass


class Transform(metaclass=TransformMeta):
    """
    Abstract base class for all transformations in TinyBaker

    :param input_paths: Dictionary of input tags to files.
    :param output_paths: Dictionary of output tags to files.
    :param optional context: The BakerDriverContext to use for this transformation
    :param optional overwrite: Whether or not to configure the transformation to overwrite output files on execution
    """

    @staticmethod
    def from_namespace(ns) -> TransformMeta:
        """
        Convert a namespace to a transform. This is currently partially
        supported, as it's undocumented and somewhat second-class to the
        standard definition path.

        :param namespace: The name
        """
        return namespace_to_transform(Transform, ns)

    @staticmethod
    def from_dict(dic) -> TransformMeta:
        """
        Convert a dictionary to a transform. This isn't intended as a
        standard developer path, but rather a helper for interop's sake

        :param namespace: The name
        """
        return dict_to_transform(Transform, dic)

    parallelism = 1

    @typechecked
    def __init__(
        self,
        input_paths: PathDict,
        output_paths: PathDict,
        overwrite: bool = False,
    ):
        _ensure_fileset_iff_fileset_tag(input_paths)
        _ensure_fileset_iff_fileset_tag(output_paths)

        self.input_paths = input_paths
        self.output_paths = output_paths

        self.input_files: FileDict = {}
        self.output_files: FileDict = {}
        self.overwrite = overwrite
        self._current_worker_context = None

    @classproperty
    def input_tags(cls):
        return {
            cls.__dict__[tag].name
            for tag in cls.__dict__
            if isinstance(cls.__dict__[tag], InputTag)
        }

    @classproperty
    def output_tags(cls):
        return {
            cls.__dict__[tag].name
            for tag in cls.__dict__
            if isinstance(cls.__dict__[tag], OutputTag)
        }

    def _init_file_dicts(self, input_paths: PathDict, output_paths: PathDict):
        if set(input_paths) != self.input_tags:
            raise FileSetError(set(input_paths), self.input_tags)

        if set(output_paths) != self.output_tags:
            raise FileSetError(set(output_paths), self.output_tags)

        input_path_set = get_files_in_path_dict(input_paths)
        output_path_set = get_files_in_path_dict(output_paths)
        intersection = set.intersection(input_path_set, output_path_set)
        if len(intersection):
            raise CircularFileSetError(
                "File included as both input and output: {}".format(
                    ", ".join(intersection)
                )
            )

        # TODO: Clean up this fileset code, like a lot
        for tag in input_paths:
            if is_fileset(tag):
                refset = []
                for individual_path in input_paths[tag]:
                    refset.append(
                        FileRef(
                            individual_path,
                            read_bit=True,
                            write_bit=False,
                            worker_context=self._current_worker_context,
                        )
                    )
                self.input_files[tag] = refset
            else:
                self.input_files[tag] = FileRef(
                    input_paths[tag],
                    read_bit=True,
                    write_bit=False,
                    worker_context=self._current_worker_context,
                )

        for tag in output_paths:
            if is_fileset(tag):
                refset = []
                for individual_path in output_paths[tag]:
                    refset.append(
                        FileRef(
                            individual_path,
                            read_bit=False,
                            write_bit=True,
                            worker_context=self._current_worker_context,
                        )
                    )
                self.output_files[tag] = refset
            else:
                self.output_files[tag] = FileRef(
                    output_paths[tag],
                    read_bit=False,
                    write_bit=True,
                    worker_context=self._current_worker_context,
                )

    def _validate_file_existence(self):
        overwrite = self.overwrite

        def ensure_input_exists(file_ref):
            if not file_ref.exists():
                raise BakerError(
                    "Referenced input path {} does not exist!".format(file_ref.path)
                )

        for tag in self.input_files:
            if is_fileset(tag):
                for path in self.input_files[tag]:
                    ensure_input_exists(path)
            else:
                ensure_input_exists(self.input_files[tag])

        def ensure_output_doesnt_exist(file_ref):
            if (not overwrite) and file_ref.exists():
                raise BakerError(
                    "Referenced output path {} already exists, and overwrite is not enabled".format(
                        file_ref.path
                    )
                )

        for tag in self.output_files:
            if is_fileset(tag):
                for path in self.output_files[tag]:
                    ensure_output_doesnt_exist(path)
            else:
                ensure_output_doesnt_exist(self.output_files[tag])

    @classmethod
    def structure(cls):
        """
        Returns a JSON-serializable dictionary describing the nested structure of the transform
        Hopefully, this is not useful beyond developing new tools for analyzing tinybaker transforms,
        e.g. you shouldn't have to use this for tinybaker to be useful. Open an issue if you do.

        :return: Dict
        """
        input_tags = list(cls.input_tags)
        input_tags.sort()
        output_tags = list(cls.output_tags)
        output_tags.sort()
        return {
            "type": "leaf",
            "name": cls.name,
            "input_tags": input_tags,
            "output_tags": output_tags,
        }

    @classproperty
    def name(cls):
        """
        The name of the transform.

        :return: string
        """
        return cls.__name__

    def run(self):
        """Run the transform instance in the default context"""
        get_default_context().run(self)

    def _exec_with_worker_context(self, worker_context):
        # Set
        input_token = input_files_ctx.set(self.input_files)
        output_token = output_files_ctx.set(self.output_files)
        try:
            self._current_worker_context = worker_context
            self._init_file_dicts(self.input_paths, self.output_paths)
            self._validate_file_existence()
            if not worker_context:
                raise SeriousErrorThatYouShouldOpenAnIssueForIfYouGet(
                    "No current worker context, somehow!"
                )
            self.script()
            self._warn_if_files_untouched()
        finally:
            input_files_ctx.reset(input_token)
            output_files_ctx.reset(output_token)

    # Only for leaf nodes!!
    def _warn_if_files_untouched(self):
        refs: List[FileRef] = []

        def add_refs(ref_dict: Dict[str, FileRef]) -> None:
            for tag in ref_dict:
                if is_fileset(tag):
                    refs.extend([(tag, ref) for ref in ref_dict[tag]])
                else:
                    refs.append((tag, ref_dict[tag]))

        add_refs(self.input_files)
        add_refs(self.output_files)
        for tag, ref in refs:
            if not ref.opened:
                warn(
                    "Unused file for tag {}: {}".format(tag, ref.path),
                    UnusedFileWarning,
                )

    @abstractmethod
    def script(self):
        """
        The script to be run on execution. This is in essence where what the transform actually does is specified
        """
        pass


# Weird type that's equivalent to Any but, also, this describes what I'm
# going for, so i'm doing it.
TransformCoercable = Union[Transform, Dict, Any]


def coerce_to_transform(coercible: TransformCoercable):
    if isinstance(coercible, TransformMeta):
        return coercible
    elif isinstance(coercible, dict):
        return Transform.from_dict(coercible)
    else:
        return Transform.from_namespace(coercible)
