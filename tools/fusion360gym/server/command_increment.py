"""

Incremental geometry construction commands

"""

import adsk.core
import adsk.fusion
import os
import sys
import importlib

# Add the common folder to sys.path
COMMON_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "common"))
if COMMON_DIR not in sys.path:
    sys.path.append(COMMON_DIR)
import name
import match
import deserialize
import serialize
importlib.reload(name)
importlib.reload(match)
importlib.reload(deserialize)
importlib.reload(serialize)
# from . import name
# from . import match
# from . import deserialize
# from . import serialize


class CommandIncrement():

    def __init__(self, runner):
        self.runner = runner
        self.logger = None
        self.app = adsk.core.Application.get()
        self.sketch_state = {}

    def set_logger(self, logger):
        self.logger = logger

    def clear(self):
        """Clear the state"""
        self.sketch_state = {}

    def add_sketch(self, data):
        """Add a sketch to the existing design"""
        design = adsk.fusion.Design.cast(self.app.activeProduct)
        if data is None or "sketch_plane" not in data:
            return self.runner.return_failure("sketch_plane not specified")
        sketch_plane = match.sketch_plane(data["sketch_plane"])
        if sketch_plane is None:
            return self.runner.return_failure("sketch_plane could not be found")
        sketches = design.rootComponent.sketches
        sketch = sketches.addWithoutEdges(sketch_plane)
        sketch_uuid = name.set_uuid(sketch)
        return self.runner.return_success({
            "sketch_id": sketch_uuid,
            "sketch_name": sketch.name
        })

    def add_point(self, data):
        """Add a point to create a new sequential line in the given sketch"""
        if (data is None or "sketch_name" not in data or
                "pt" not in data):
            return self.runner.return_failure("add_point data not specified")
        sketch = match.sketch_by_name(data["sketch_name"])
        if sketch is None:
            return self.runner.return_failure("sketch not found")
        sketch_uuid = name.get_uuid(sketch)
        # If this is the first point, store it and return
        if sketch.name not in self.sketch_state:
            self.__init_sketch_state(sketch.name, data["pt"], data["pt"])
            profile_data = serialize.sketch_profiles(sketch.profiles)
            return self.runner.return_success({
                "sketch_id": sketch_uuid,
                "sketch_name": sketch.name,
                "profiles": profile_data
            })
        state = self.sketch_state[sketch.name]
        transform = data["transform"] if "transform" in data else None
        return self.__add_line(sketch, sketch_uuid, state["last_pt"], data["pt"], transform)

    def add_line(self, data):
        """Add a line to an existing sketch"""
        if (data is None or "sketch_name" not in data or
                "pt1" not in data or "pt2" not in data):
            return self.runner.return_failure("add_line data not specified")
        sketch = match.sketch_by_name(data["sketch_name"])
        if sketch is None:
            return self.runner.return_failure("sketch not found")
        sketch_uuid = name.get_uuid(sketch)
        transform = data["transform"] if "transform" in data else None
        return self.__add_line(sketch, sketch_uuid, data["pt1"], data["pt2"], transform)

    def close_profile(self, data):
        """Close the current set of lines to create one or more profiles
           by joining the first point to the last"""
        if data is None or "sketch_name" not in data:
            return self.runner.return_failure("close_profile data not specified")
        sketch = match.sketch_by_name(data["sketch_name"])
        if sketch is None:
            return self.runner.return_failure("sketch not found")
        sketch_uuid = name.get_uuid(sketch)
        if sketch.name not in self.sketch_state:
            return self.runner.return_failure("sketch state not found")
        state = self.sketch_state[sketch.name]
        # We need at least 4 points (2 lines with 2 points each)
        if state["pt_count"] < 4:
            return self.runner.return_failure("sketch has too few points")
        if state["last_pt"] is None or state["first_pt"] is None:
            return self.runner.return_failure("sketch end points invalid")
        return self.__add_line(sketch, sketch_uuid, state["last_pt"], state["first_pt"])

    def add_extrude(self, data):
        """Add an extrude feature from a sketch"""
        if (data is None or "sketch_name" not in data or
                "profile_id" not in data or "distance" not in data or
                "operation" not in data):
            return self.runner.return_failure("add_extrude data not specified")
        sketch = match.sketch_by_name(data["sketch_name"])
        if sketch is None:
            return self.runner.return_failure("extrude sketch not found")
        profile = match.sketch_profile_by_id(data["profile_id"], [sketch])
        if profile is None:
            return self.runner.return_failure("extrude sketch profile not found")
        operation = self.__get_extrude_operation(data["operation"])
        if operation is None:
            return self.runner.return_failure("extrude operation not found")

        # Make the extrude
        design = adsk.fusion.Design.cast(self.app.activeProduct)
        extrudes = design.rootComponent.features.extrudeFeatures
        extrude_input = extrudes.createInput(profile, operation)
        distance = adsk.core.ValueInput.createByReal(data["distance"])
        extent_distance = adsk.fusion.DistanceExtentDefinition.create(distance)
        extrude_input.setOneSideExtent(extent_distance, adsk.fusion.ExtentDirections.PositiveExtentDirection)
        extrude_feature = extrudes.add(extrude_input)
        # Serialize the data and return
        extrude_feature_data = serialize.extrude_feature_brep(extrude_feature)
        return self.runner.return_success(extrude_feature_data)

    def __add_line(self, sketch, sketch_uuid, pt1, pt2, transform=None):
        start_point = deserialize.point3d(pt1)
        end_point = deserialize.point3d(pt2)
        if transform is not None:
            # For mapping Fusion exported data back correctly
            xform = deserialize.matrix3d(transform)
            sketch_transform = sketch.transform
            sketch_transform.invert()
            xform.transformBy(sketch_transform)
            start_point.transformBy(xform)
            end_point.transformBy(xform)

        line = sketch.sketchCurves.sketchLines.addByTwoPoints(start_point, end_point)
        curve_uuid = name.set_uuid(line)
        name.set_uuids_for_sketch(sketch)
        profile_data = serialize.sketch_profiles(sketch.profiles)
        if sketch.name not in self.sketch_state:
            self.__init_sketch_state(sketch.name, pt1, pt2)
        else:
            self.__inc_sketch_state(sketch.name, pt2)
        return self.runner.return_success({
            "sketch_id": sketch_uuid,
            "sketch_name": sketch.name,
            "curve_id": curve_uuid,
            "profiles": profile_data
        })

    def __get_extrude_operation(self, operation):
        """Return an appropriate extrude operation"""
        design = adsk.fusion.Design.cast(self.app.activeProduct)
        # Check that the operation is going to work
        body_count = 0
        for component in design.allComponents:
            body_count += component.bRepBodies.count
        # If there are no other bodies, we have to make a new body
        if body_count == 0:
            operation = "NewBodyFeatureOperation"
        return deserialize.feature_operations(operation)

    def __init_sketch_state(self, sketch_name, first_pt=None, last_pt=None, pt_count=0):
        """Initialize the sketch state"""
        self.sketch_state[sketch_name] = {
            "first_pt": first_pt,
            "last_pt": last_pt,
            "pt_count": pt_count
        }

    def __inc_sketch_state(self, sketch_name, last_pt):
        """Increment the sketch state with the latest point"""
        state = self.sketch_state[sketch_name]
        state["last_pt"] = last_pt
        # Increment by 2 as we are adding a curve
        state["pt_count"] += 2