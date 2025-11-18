#!/usr/bin/env python3
import sys
from collections import defaultdict
from shapely.geometry import MultiPoint, Polygon
from shapely.ops import unary_union

def concave_hull(points, alpha=0.5):
    """Generate a concave hull using alphashape"""
    if len(points) < 4:
        return MultiPoint(points).convex_hull
    
    try:
        import alphashape
        # Convert to list if needed - this was the bug!
        points_list = list(points) if not isinstance(points, list) else points
        hull = alphashape.alphashape(points_list, alpha)
        
        if hull.geom_type == 'Polygon':
            return hull
        elif hull.geom_type == 'MultiPolygon':
            # Take the largest polygon if we get multiple
            return max(hull.geoms, key=lambda p: p.area)
        else:
            # Fallback to convex hull
            return MultiPoint(points_list).convex_hull
    except Exception as e:
        print(f"  Alphashape failed ({e}), using convex hull")
        return MultiPoint(list(points)).convex_hull

def simplify_polygon(poly, max_points=40):
    """Simplify polygon to max_points"""
    if not isinstance(poly, Polygon):
        return poly
    
    tolerance = 0.1
    simplified = poly.simplify(tolerance, preserve_topology=True)
    
    # Increase tolerance until we're under max_points
    while len(list(simplified.exterior.coords)) > max_points and tolerance < 10:
        tolerance += 0.5
        simplified = poly.simplify(tolerance, preserve_topology=True)
    
    return simplified

def extract_xy(line):
    """Extract X and Y coordinates from G-code line"""
    x = y = None
    for part in line.split():
        if part.startswith("X"):
            try:
                x = float(part[1:])
            except:
                pass
        if part.startswith("Y"):
            try:
                y = float(part[1:])
            except:
                pass
    return x, y

def main():
    # Handle both manual mode (2 args) and OrcaSlicer mode (1 arg)
    if len(sys.argv) == 2:
        # OrcaSlicer mode: modify file in-place
        infile = sys.argv[1]
        outfile = sys.argv[1]
    elif len(sys.argv) == 3:
        # Manual mode: separate input/output
        infile = sys.argv[1]
        outfile = sys.argv[2]
    else:
        print("Usage: python3 exact_outline_post.py input.gcode [output.gcode]")
        sys.exit(1)
    
    objects = {}  # id -> {"name": str, "points": [], "center": [x,y]}
    output_lines = []
    current_object_id = None
    in_support = False
    insert_position = None  # Track where to insert EXCLUDE_OBJECT_DEFINE commands
    
    with open(infile, "r") as f:
        lines = f.readlines()
    
    for i, line in enumerate(lines):
        ls = line.strip()
        output_lines.append(line)
        
        # Find insertion point - after initial comments but before first real G-code
        if insert_position is None and ls and not ls.startswith(';') and (ls.startswith('G') or ls.startswith('M')):
            insert_position = len(output_lines) - 1  # Insert before this line
        
        # Detect when we start printing an object
        if ls.startswith("; printing object"):
            in_support = False
            parts = ls.split()
            
            # Extract name (between "object" and "id:")
            name_start = ls.find("object") + 7
            id_start = ls.find("id:")
            if name_start > 7 and id_start > 0:
                name = ls[name_start:id_start].strip()
                
                # Extract ID
                try:
                    obj_id = int(ls.split("id:")[1].split()[0])
                except:
                    continue
                
                current_object_id = obj_id
                if current_object_id not in objects:
                    objects[current_object_id] = {"name": name, "points": set(), "center": None}
                    print(f"Found object: {name} (id: {obj_id})")
            continue
        
        # Detect when we stop printing an object
        if ls.startswith("; stop printing object"):
            current_object_id = None
            continue
        
        # Check if we're in support section
        if ";TYPE:Support" in ls or ";type:support" in ls.lower():
            in_support = True
            continue
        
        # Reset support flag on new type
        if ";TYPE:" in ls and not ";TYPE:Support" in ls:
            in_support = False
        
        # Collect XY moves only when actively printing an object (not support)
        if current_object_id is not None and not in_support:
            if ls.startswith("G1") or ls.startswith("G0"):
                x, y = extract_xy(ls)
                if x is not None and y is not None:
                    # Use set to avoid duplicate points
                    objects[current_object_id]["points"].add((round(x, 2), round(y, 2)))
    
    # Generate polygons in EXCLUDE_OBJECT_DEFINE format
    name_counts = defaultdict(int)
    define_commands = []
    
    for obj_id, obj_data in objects.items():
        name = obj_data["name"]
        copy_number = name_counts[name]
        name_counts[name] += 1
        
        points = list(obj_data["points"])
        
        if len(points) < 3:
            print(f"Warning: Object {name} has too few points ({len(points)}), skipping")
            continue
        
        print(f"Processing {name} (copy {copy_number}): {len(points)} unique points")
        
        try:
            # Create concave hull - alpha controls tightness (lower = tighter)
            hull = concave_hull(points, alpha=0.5)
            
            if hull is None or hull.is_empty:
                print(f"  Warning: Could not create hull for {name}")
                continue
            
            # Simplify to max 40 points
            hull = simplify_polygon(hull, max_points=40)
            
            # Get coordinates
            coords = list(hull.exterior.coords)
            
            # Calculate center point
            center_x = sum(x for x, y in coords) / len(coords)
            center_y = sum(y for x, y in coords) / len(coords)
            
            # Format as Klipper EXCLUDE_OBJECT_DEFINE command
            # POLYGON format: [[x,y],[x,y],...] (JSON array)
            polygon_str = "[[" + "],[".join(f"{round(x, 3)},{round(y, 3)}" for x, y in coords) + "]]"
            
            # Create unique name for copies: "name_copy0", "name_copy1", etc.
            unique_name = f"{name}_copy{copy_number}" if copy_number > 0 else name
            
            define_cmd = (
                f'EXCLUDE_OBJECT_DEFINE NAME="{unique_name}" '
                f'CENTER={round(center_x, 3)},{round(center_y, 3)} '
                f'POLYGON={polygon_str}\n'
            )
            
            define_commands.append(define_cmd)
            
            print(f"  Created polygon with {len(coords)} points")
            
        except Exception as e:
            print(f"  Error processing {name}: {e}")
            continue
    
    # Insert EXCLUDE_OBJECT_DEFINE commands at the beginning
    if insert_position is None:
        insert_position = 0  # Fallback to very beginning
    
    if define_commands:
        output_lines.insert(insert_position, "\n; Generated by exact_outline_post.py\n")
        for cmd in reversed(define_commands):  # Reverse to maintain order
            output_lines.insert(insert_position + 1, cmd)
        output_lines.insert(insert_position + 1 + len(define_commands), "\n")
    
    # Write output
    with open(outfile, "w") as f:
        f.writelines(output_lines)
    
    print(f"\n✓ Processed {len(objects)} objects")
    print(f"✓ Output written to {outfile}")

if __name__ == "__main__":
    main()