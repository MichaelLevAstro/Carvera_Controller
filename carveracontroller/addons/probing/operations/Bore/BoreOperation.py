import copy

from carveracontroller.addons.probing.operations.Bore.BoreParameterDefinitions import BoreParameterDefinitions
from carveracontroller.addons.probing.operations.OperationsBase import OperationsBase, ProbeSettingDefinition
from carveracontroller.addons.probing.operations.SingleAxis.SingleAxisProbeParameterDefinitions import (
    SingleAxisProbeParameterDefinitions,
)


class BoreOperation(OperationsBase):
    imagePath: str

    def __init__(self, title, requires_x, requires_y, image_path, **kwargs):
        self.title = title
        self.imagePath = image_path
        self.requires_x = requires_x
        self.requires_y = requires_y

    def generate(self, input_config: dict[str, float]):
        config = copy.deepcopy(input_config)

        if not self.requires_x:
            config[BoreParameterDefinitions.XAxisDistance.code] = ""
        if not self.requires_y:
            config[BoreParameterDefinitions.YAxisDistance.code] = ""
        if not (self.requires_x and self.requires_y):
            # P only feeds the host-side multi-point circle fit, which handles
            # the full round-bore case. The single-axis M461 goes to the
            # firmware, which has no P parameter.
            config[BoreParameterDefinitions.SamplePoints.code] = ""

        return "M461" + self.config_to_gcode(config)

    def get_missing_config(self, config: dict[str, float]):
        if self.requires_x:
            definition = BoreParameterDefinitions.XAxisDistance
            if definition.code not in config or not config[definition.code].strip():
                return definition
        if self.requires_y:
            definition = BoreParameterDefinitions.YAxisDistance
            if definition.code not in config or not config[definition.code].strip():
                return definition

        required_definitions = {
            name: value
            for name, value in BoreParameterDefinitions.__dict__.items()
            if isinstance(value, ProbeSettingDefinition) and value.is_required
        }

        return super().validate_required(required_definitions, config)
