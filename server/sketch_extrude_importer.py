import adsk.core
import adsk.fusion
import traceback
import json
import os
import sys
import time
import math
from pathlib import Path
from importlib import reload

from . import deserialize
reload(deserialize)


class SketchExtrudeImporter():
    def __init__(self, json_data):
        self.app = adsk.core.Application.get()

        if isinstance(json_data, dict):
            self.data = json_data
        else:
            with open(json_data, encoding="utf8") as f:
                self.data = json.load(f)

        product = self.app.activeProduct
        self.design = adsk.fusion.Design.cast(product)

    def reconstruct(self):
        # Keep track of the sketch profiles
        sketch_profiles = {}
        for timeline_object in self.data["timeline"]:
            entity_uuid = timeline_object["entity"]
            entity_index = timeline_object["index"]
            entity = self.data["entities"][entity_uuid]
            print("Reconstructing", entity["name"])
            if entity["type"] == "Sketch":
                sketch_profile_set = self.reconstruct_sketch(entity, sketch_profiles)
                if sketch_profile_set:
                    sketch_profiles.update(**sketch_profile_set)

            elif entity["type"] == "ExtrudeFeature":
                self.reconstruct_extrude_feature(entity, sketch_profiles)

    def find_profile(self, reconstruced_profiles, profile_uuid, profile_data, xform):
        # Sketch profiles are automatically generated by Fusion
        # After we have added the curves we have to traverse the profiles
        # to find one with all of the curve uuids from the original
        sorted_curve_uuids = self.get_curve_uuids(profile_data)
        # print(f"Finding profile {profile_uuid} with {len(sorted_curve_uuids)} curves")
        for index, profile_dict in enumerate(reconstruced_profiles):
            profile = profile_dict["profile"]
            sorted_found_curve_uuids = profile_dict["curve_uuids"]
            if sorted_found_curve_uuids == sorted_curve_uuids and self.are_profile_properties_identical(profile, profile_data, xform):
                print(f"Profile found with {len(sorted_curve_uuids)} curve uuids")
                return profile, index
        print(f"Profile not found: {profile_uuid} with {len(sorted_curve_uuids)} curves")
        return None, -1

    def are_profile_properties_identical(self, profile, profile_data, xform):
        profile_props = profile.areaProperties(adsk.fusion.CalculationAccuracy.HighCalculationAccuracy)
        tolerance = 0.000001
        if not math.isclose(profile_props.area, profile_data["properties"]["area"], abs_tol=tolerance):
            print("Profile area doesn't match")
            return False
        if not math.isclose(profile_props.perimeter, profile_data["properties"]["perimeter"], abs_tol=tolerance):
            print("Profile perimeter doesn't match")
            return False
        centroid_point = deserialize.point3d(profile_data["properties"]["centroid"])
        centroid_point.transformBy(xform)
        if not math.isclose(profile_props.centroid.x, centroid_point.x, abs_tol=tolerance):
            print("Centroid.x doesn't match")
            return False
        if not math.isclose(profile_props.centroid.y, centroid_point.y, abs_tol=tolerance):
            print("Centroid.y doesn't match")
            return False
        if not math.isclose(profile_props.centroid.z, centroid_point.z, abs_tol=tolerance):
            print("Centroid.z doesn't match")
            return False
        return True

    def get_profile_curve_uuids(self, sketch):
        print("Reconstructed profiles------------------")
        reconstructed_profiles = []
        for profile in sketch.profiles:
            # We use a set as there can be duplicate curves in the list
            found_curve_uuids = set()
            for loop in profile.profileLoops:
                for curve in loop.profileCurves:
                    sketch_ent = curve.sketchEntity
                    curve_uuid = self.get_uuid(sketch_ent)
                    if curve_uuid is not None:
                        found_curve_uuids.add(curve_uuid)
            sorted_found_curve_uuids = sorted(list(found_curve_uuids))
            reconstructed_profiles.append({
                "profile": profile,
                "curve_uuids": sorted_found_curve_uuids
            })
            print(len(sorted_found_curve_uuids), sorted_found_curve_uuids)
        print("---------------------------------------")
        return reconstructed_profiles

    def get_uuid(self, entity):
        uuid_att = entity.attributes.itemByName("Dataset", "uuid")
        if uuid_att is not None:
            return uuid_att.value
        else:
            return None

    def set_uuid(self, entity, unique_id):
        uuid_att = entity.attributes.itemByName("Dataset", "uuid")
        if uuid_att is None:
            entity.attributes.add("Dataset", "uuid", unique_id)

    def get_curve_uuids(self, profile_data):
        loops = profile_data["loops"]
        # Use a set to remove duplicates
        curve_uuids = set()
        for loop in loops:
            profile_curves = loop["profile_curves"]
            for profile_curve in profile_curves:
                curve_uuids.add(profile_curve["curve"])
        return sorted(list(curve_uuids))

    def find_transform_for_sketch_geom(self, sketch_transform, original_transform_json):
        # The sketch transform operates on a sketch point p_sketch and transforms it into
        # world space (or at least the space of the assembly context)
        #
        # p_world = T * p_sketch
        #
        # Now we need to cope with the sketch plane having two different transforms when we 
        # extract and when we import it.  
        # 
        # We know the one thing which stays constant is the final point in world space, so
        # we have
        #
        # p_world = T_extract * p_sketch = T_import * T_correction * p_sketch
        #
        # hence
        #
        # T_extract = T_import * T_correction 
        #
        # Now premultiplying both sides by T_import^-1 gives us
        #
        # T_correction = T_import^-1  * T_extract 
        #
        # This function need to compute T_correction
        
        # sketch_transform is T_import.    Here we find T_import^-1
        ok = sketch_transform.invert()
        assert ok

        # Set xform = T_extract 
        xform = deserialize.matrix3d(original_transform_json)
        
        # The transformBy() function must be "premultiply"
        # so here we have
        # xform = T_import^-1  * T_extract 
        xform.transformBy(sketch_transform)
        return xform

    def reconstruct_sketch(self, sketch_data, sketch_profiles):
        # Skip empty sketches
        if "curves" not in sketch_data or "profiles" not in sketch_data or "points" not in sketch_data:
            return None

        sketches = self.design.rootComponent.sketches
        # Find the right sketch plane to use
        sketch_plane = self.get_sketch_plane(sketch_data["reference_plane"], sketch_profiles)
        sketch = sketches.addWithoutEdges(sketch_plane)
        
        # Create an identity matrix
        transform_for_sketch_geom = adsk.core.Matrix3D.create()
        # We will need to apply some other transform to the sketch data
        sketch_transform = sketch.transform
        transform_for_sketch_geom = self.find_transform_for_sketch_geom(sketch_transform, sketch_data["transform"])
            
        # Draw exactly what the user drew and then search for the profiles
        new_sketch_profiles = self.reconstruct_curves(sketch, sketch_data, transform_for_sketch_geom)
        adsk.doEvents()
        return new_sketch_profiles

    def get_sketch_plane(self, reference_plane, sketch_profiles):
        # ConstructionPlane as reference plane
        if reference_plane["type"] == "ConstructionPlane" and "name" in reference_plane:
            sketch_plane = deserialize.construction_plane(reference_plane["name"])
            if sketch_plane is not None:
                print(f"Sketch plane (Construction Plane) - {reference_plane['name']}")
                return sketch_plane
        # BRepFace as reference plane
        elif reference_plane["type"] == "BRepFace" and "point_on_face" in reference_plane:
            face = deserialize.face_by_point3d(reference_plane["point_on_face"])
            if face is not None:
                if face.geometry.surfaceType == adsk.core.SurfaceTypes.PlaneSurfaceType:
                    print(f"Sketch plane (BRepFace) - {face.tempId}")
                    return face
                else:
                    print(f"Sketch plane (BRepFace) - invalid surface type {face.geometry.surfaceType}")
            else:
                print("Sketch plane point on face not found!")
        # Sketch Profile as reference plane
        elif reference_plane["type"] == "Profile" and "profile" in reference_plane:
            profile_uuid = reference_plane["profile"]
            if profile_uuid in sketch_profiles:
                profile = sketch_profiles[profile_uuid]
                print(f"Sketch plane (Profile) - {profile_uuid}")
                # We could reference the original sketch plane like this:
                # return profile.parentSketch.referencePlane
                # But the sketch plane can differ from the profile plane
                
                # Note: The API doesn't support creating references to sketch profiles directly
                # So instead we create a construction plane from the profile and use that
                # This preserves the reference indirectly through the construction plane
                planes = self.design.rootComponent.constructionPlanes
                plane_input = planes.createInput()
                offset_distance = adsk.core.ValueInput.createByReal(0)
                plane_input.setByOffset(profile, offset_distance)
                plane = planes.add(plane_input)
                return plane

        print(f"Sketch plane - DEFAULT XY")
        return self.design.rootComponent.xYConstructionPlane

    def reconstruct_curves(self, sketch, sketch_data, xform):
        print(len(sketch_data["points"]), "points,", len(sketch_data["curves"]), "curves")
        # Turn off sketch compute until we add all the curves
        sketch.isComputeDeferred = True
        self.reconstruct_sketch_curves(sketch, sketch_data["curves"], sketch_data["points"], xform)
        sketch.isComputeDeferred = False

        # If we draw the user curves we have to recover the profiles that Fusion generates
        # First pull out the list of reconstructed profile curve uuids
        reconstructed_profiles = self.get_profile_curve_uuids(sketch)
        sketch_profiles = {}
        missing_profiles = {}
        # We first try and find exact matches
        # i.e. a profile with the same set of (deduplicated) curve ids
        # and with an area/perimeter/centroid that matches
        for profile_uuid, profile_data in sketch_data["profiles"].items():
            # print("Finding profile", profile_data["profile_uuid"])
            sketch_profile, reconstructed_profile_index = self.find_profile(reconstructed_profiles, profile_uuid, profile_data, xform)
            if sketch_profile is not None:
                sketch_profiles[profile_uuid] = sketch_profile
                # Remove the matched profile from the pool
                del reconstructed_profiles[reconstructed_profile_index]
            else:
                missing_profiles[profile_uuid] = profile_data
        
        # Sometimes the exact match will fail, so we search for the most 'similar' profile,
        # with the most common curve uuids, remaining in the reconstructed profile set
        missing_profile_count = len(missing_profiles)
        if missing_profile_count > 0:
            print(f"{missing_profile_count} Missing profiles and {len(reconstructed_profiles)} remaining reconstructed profiles")
            matched_profiles = 0
            for missing_profile_uuid, missing_profile_data in missing_profiles.items():
                best_match_profile = self.get_closest_profile(missing_profile_data, reconstructed_profiles, missing_profile_uuid)
                if best_match_profile is not None:
                    sketch_profiles[missing_profile_uuid] = best_match_profile
                    matched_profiles += 1
            
            unmatched_profiles = missing_profile_count - matched_profiles
            if unmatched_profiles > 0:
                print(f"{unmatched_profiles} left over unmatched profiles!")

        return sketch_profiles

    def get_closest_profile(self, missing_profile_data, reconstructed_profiles, missing_profile_uuid):
        """Try and find the closest profile match based on overlap of curve ids"""
        if len(reconstructed_profiles) == 1:
            return reconstructed_profiles[0]["profile"]
        sorted_curve_uuids = self.get_curve_uuids(missing_profile_data)
        sorted_curve_uuids_count = len(sorted_curve_uuids)
        max_score = 0
        best_match_index = -1
        for index, reconstructed_profile in enumerate(reconstructed_profiles):
            overlap = self.get_profile_curve_overlap_count(sorted_curve_uuids, reconstructed_profile["curve_uuids"])
            reconstructed_profile_curve_uuids_coint = len(reconstructed_profile["curve_uuids"])
            score = overlap - abs(reconstructed_profile_curve_uuids_coint-sorted_curve_uuids_count)
            print(f"Score: {score} - {sorted_curve_uuids_count} vs {reconstructed_profile_curve_uuids_coint}")
            if score > max_score:
                best_match_index = index
                max_score = score
        if best_match_index >= 0:
            print(f"""Matching profile {missing_profile_uuid} with {sorted_curve_uuids_count} curves
                to a left over reconstructed profile with {len(reconstructed_profiles[best_match_index]["curve_uuids"])} curves""")
            return reconstructed_profiles[best_match_index]["profile"]
        else:
            return None

    def get_profile_curve_overlap_count(self, original, reconstructed):
        intersection = set(original) & set(reconstructed)
        return len(intersection)

    def reconstruct_sketch_curves(self, sketch, curves_data, points_data, xform):
        for curve_uuid, curve in curves_data.items():
            # Don't bother generating construction geometry
            if curve["construction_geom"]:
                continue
            if curve["type"] == "SketchLine":
                self.reconstruct_sketch_line(sketch.sketchCurves.sketchLines, curve, curve_uuid, points_data, xform)
            elif curve["type"] == "SketchArc":
                self.reconstruct_sketch_arc(sketch.sketchCurves.sketchArcs, curve, curve_uuid, points_data, xform)
            elif curve["type"] == "SketchCircle":
                self.reconstruct_sketch_circle(sketch.sketchCurves.sketchCircles, curve, curve_uuid, points_data, xform)
            elif curve["type"] == "SketchFittedSpline":
                self.reconstruct_sketch_fitted_spline(sketch.sketchCurves.sketchFittedSplines, curve, curve_uuid, xform)
            else:
                print("Unsupported curve type", curve["type"])

    def reconstruct_sketch_line(self, sketch_lines, curve_data, curve_uuid, points_data, xform):
        start_point_uuid = curve_data["start_point"]
        end_point_uuid = curve_data["end_point"]
        start_point = deserialize.point3d(points_data[start_point_uuid])
        end_point = deserialize.point3d(points_data[end_point_uuid])
        start_point.transformBy(xform)
        end_point.transformBy(xform)
        line = sketch_lines.addByTwoPoints(start_point, end_point)
        self.set_uuid(line, curve_uuid)

    def reconstruct_sketch_arc(self, sketch_arcs, curve_data, curve_uuid, points_data, xform):
        start_point_uuid = curve_data["start_point"]
        center_point_uuid = curve_data["center_point"]
        start_point = deserialize.point3d(points_data[start_point_uuid])
        center_point = deserialize.point3d(points_data[center_point_uuid])
        start_point.transformBy(xform)
        center_point.transformBy(xform)
        sweep_angle = curve_data["end_angle"] - curve_data["start_angle"]
        arc = sketch_arcs.addByCenterStartSweep(center_point, start_point, sweep_angle)
        self.set_uuid(arc, curve_uuid)

    def reconstruct_sketch_circle(self, sketch_circles, curve_data, curve_uuid, points_data, xform):
        center_point_uuid = curve_data["center_point"]
        center_point = deserialize.point3d(points_data[center_point_uuid])
        center_point.transformBy(xform)
        radius = curve_data["radius"]
        circle = sketch_circles.addByCenterRadius(center_point, radius)
        self.set_uuid(circle, curve_uuid)

    def reconstruct_sketch_fitted_spline(self, sketch_fitted_splines, curve_data, curve_uuid, xform):
        nurbs_curve = self.reconstruct_nurbs_curve(curve_data, xform)
        spline = sketch_fitted_splines.addByNurbsCurve(nurbs_curve)
        self.set_uuid(spline, curve_uuid)

    def reconstruct_nurbs_curve(self, curve_data, xform):
        control_points = deserialize.point3d_list(curve_data["control_points"], xform)
        nurbs_curve = None
        if curve_data["rational"] is True:
            nurbs_curve = adsk.core.NurbsCurve3D.createRational(
                control_points, curve_data["degree"],
                curve_data["knots"], curve_data["weights"],
                curve_data["periodic"]
            )
        else:
            nurbs_curve = adsk.core.NurbsCurve3D.createNonRational(
                control_points, curve_data["degree"],
                curve_data["knots"], curve_data["periodic"]
            )
        return nurbs_curve

    def reconstruct_extrude_feature(self, extrude_data, sketch_profiles):
        extrudes = self.design.rootComponent.features.extrudeFeatures

        # There can be more than one profile, so we create an object collection
        extrude_profiles = adsk.core.ObjectCollection.create()
        for profile in extrude_data["profiles"]:
            profile_uuid = profile["profile"]
            # print('Profile uuid:', profile_uuid)
            extrude_profiles.add(sketch_profiles[profile_uuid])

        # The operation defines if the extrusion becomes a new body
        # a new component or cuts/joins another body (i.e. boolean operation)
        operation = deserialize.feature_operations(extrude_data["operation"])
        extrude_input = extrudes.createInput(extrude_profiles, operation)

        # Simple extrusion in one direction
        if extrude_data["extent_type"] == "OneSideFeatureExtentType":
            self.set_one_side_extrude_input(extrude_input, extrude_data["extent_one"])
        # Extrusion in two directions with different distances
        elif extrude_data["extent_type"] == "TwoSidesFeatureExtentType":
            self.set_two_side_extrude_input(extrude_input, extrude_data["extent_one"], extrude_data["extent_two"])
        # Symmetrical extrusion by the same distance on each side
        elif extrude_data["extent_type"] == "SymmetricFeatureExtentType":
            self.set_symmetric_extrude_input(extrude_input, extrude_data["extent_one"])

        # The start extent is initialized to be the profile plane
        # but we may need to change it to an offset
        # after all other changes
        self.set_start_extent(extrude_input, extrude_data["start_extent"])       
        return extrudes.add(extrude_input)

    def set_start_extent(self, extrude_input, start_extent):
        # Only handle the offset case
        # ProfilePlaneStartDefinition is already setup
        # and other cases we don't handle
        if start_extent["type"] == "OffsetStartDefinition":
            offset_distance = adsk.core.ValueInput.createByReal(start_extent["offset"]["value"])
            offset_start_def = adsk.fusion.OffsetStartDefinition.create(offset_distance)
            extrude_input.startExtent = offset_start_def

    def set_one_side_extrude_input(self, extrude_input, extent_one):
        distance = adsk.core.ValueInput.createByReal(extent_one["distance"]["value"])
        extent_distance = adsk.fusion.DistanceExtentDefinition.create(distance)
        taper_angle = adsk.core.ValueInput.createByReal(0)
        if "taper_angle" in extent_one:
            taper_angle = adsk.core.ValueInput.createByReal(extent_one["taper_angle"]["value"])
        extrude_input.setOneSideExtent(extent_distance, adsk.fusion.ExtentDirections.PositiveExtentDirection, taper_angle)

    def set_two_side_extrude_input(self, extrude_input, extent_one, extent_two):
        distance_one = adsk.core.ValueInput.createByReal(extent_one["distance"]["value"])
        distance_two = adsk.core.ValueInput.createByReal(extent_two["distance"]["value"])
        extent_distance_one = adsk.fusion.DistanceExtentDefinition.create(distance_one)
        extent_distance_two = adsk.fusion.DistanceExtentDefinition.create(distance_two)
        taper_angle_one = adsk.core.ValueInput.createByReal(0)
        taper_angle_two = adsk.core.ValueInput.createByReal(0)
        if "taper_angle" in extent_one:
            taper_angle_one = adsk.core.ValueInput.createByReal(extent_one["taper_angle"]["value"])
        if "taper_angle" in extent_two:
            taper_angle_two = adsk.core.ValueInput.createByReal(extent_two["taper_angle"]["value"])
        extrude_input.setTwoSidesExtent(extent_distance_one, extent_distance_two, taper_angle_one, taper_angle_two)

    def set_symmetric_extrude_input(self, extrude_input, extent_one):
        # SYMMETRIC EXTRUDE
        # Symmetric extent is currently buggy when a taper is applied
        # So instead we use a two sided extent with symmetry
        # Note that the distance is not a DistanceExtentDefinition
        # distance = adsk.core.ValueInput.createByReal(extent_one["distance"]["value"])
        # taper_angle = adsk.core.ValueInput.createByReal(0)
        # if "taper_angle" in extent_one:
        #     taper_angle = adsk.core.ValueInput.createByReal(extent_one["taper_angle"]["value"])
        # is_full_length = extent_one["is_full_length"]
        # extrude_input.setSymmetricExtent(distance, is_full_length, taper_angle)
        #
        # TWO SIDED EXTRUDE WORKAROUND
        distance = extent_one["distance"]["value"]
        if extent_one["is_full_length"]:
            distance = distance * 0.5
        distance_one = adsk.core.ValueInput.createByReal(distance)
        distance_two = adsk.core.ValueInput.createByReal(distance)
        extent_distance_one = adsk.fusion.DistanceExtentDefinition.create(distance_one)
        extent_distance_two = adsk.fusion.DistanceExtentDefinition.create(distance_two)
        taper_angle_one = adsk.core.ValueInput.createByReal(0)
        taper_angle_two = adsk.core.ValueInput.createByReal(0)
        if "taper_angle" in extent_one:
            taper_angle_one = adsk.core.ValueInput.createByReal(extent_one["taper_angle"]["value"])
            taper_angle_two = adsk.core.ValueInput.createByReal(extent_one["taper_angle"]["value"])
        extrude_input.setTwoSidesExtent(extent_distance_one, extent_distance_two, taper_angle_one, taper_angle_two)
