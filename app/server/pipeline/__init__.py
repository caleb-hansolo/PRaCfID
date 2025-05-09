import ollama
import cv2
import numpy as np
import re
import os
import zipfile
import io
import shutil
import time
import logging

logger = logging.getLogger(__name__)

# Removed global constants for thresholds, they will be passed as parameters.
# PATTERN_DIR, PATTERN_IMAGES, PATTERN_NAMES are also removed as patterns will be loaded dynamically.

def is_mostly_black_or_white(image_cv, black_threshold_val=30, white_threshold_val=225, percentage_threshold=0.60):
    """
    Check if an image is mostly black or white.
    Args:
        image_cv (numpy.ndarray): Image data from cv2.imread or a frame.
        black_threshold_val (int): Pixel intensity below this is considered black.
        white_threshold_val (int): Pixel intensity above this is considered white.
        percentage_threshold (float): Threshold for black or white percentage.
    Returns:
        bool: True if the image is mostly black or white, False otherwise.
    """
    if image_cv is None:
        logger.error("is_mostly_black_or_white: Input image is None.")
        return False # Or raise error

    gray_image = image_cv
    if len(image_cv.shape) == 3:
        gray_image = cv2.cvtColor(image_cv, cv2.COLOR_BGR2GRAY)

    total_pixels = gray_image.size
    if total_pixels == 0:
        logger.error("is_mostly_black_or_white: Image has zero pixels.")
        return False

    black_pixels = np.sum(gray_image <= black_threshold_val)
    white_pixels = np.sum(gray_image >= white_threshold_val)

    black_percentage = black_pixels / total_pixels
    white_percentage = white_pixels / total_pixels

    return black_percentage >= percentage_threshold or white_percentage >= percentage_threshold

def modelObjectDetection(frame_cv, model_prompt_content):
    """
    Detect man-made objects in an image using a pre-trained model from Ollama.
    Args:
        frame_cv (numpy.ndarray): Image data (frame).
        model_prompt_content (str): The prompt for the LLM.
    Returns:
        tuple: (bool indicating if man-made objects detected, str response from model)
    """
    if frame_cv is None:
        logger.error("modelObjectDetection: Input frame is None.")
        return True, "Error: Input frame was None." # Default to objects_detected = True to avoid filtering good images due to error

    objects_detected = True  # Default assumption
    model_response_content = "Error in model processing."

    try:
        _, buffer = cv2.imencode('.jpg', frame_cv)
        image_bytes = buffer.tobytes()

        res = ollama.chat(
            model="llava:34b", # Ensure this model is available
            messages=[
                {
                    'role': 'user',
                    'content': model_prompt_content,
                    'images': [image_bytes]
                }
            ]
        )
        model_response_content = res['message']['content']
        if re.search(r'False', model_response_content, re.IGNORECASE): # More robust check
            objects_detected = False
    except Exception as e:
        logger.error(f"Error in modelObjectDetection with Ollama: {e}")
        # Keep objects_detected = True to be safe, or handle error differently
    return objects_detected, model_response_content


def patternThresholding(test_image_cv, loaded_pattern_images_data, threshold_match_val):
    """
    Compare an image against multiple patterns using SIFT features.
    Args:
        test_image_cv (numpy.ndarray): Grayscale image to be matched.
        loaded_pattern_images_data (list): List of tuples (pattern_cv_gray, pattern_name).
        threshold_match_val (int): SIFT match distance threshold (lower is stricter, but it's used differently here).
                                The original code counts matches with distance < Threshold.
                                The variable was THRESHOLD_PATTERN_MATCH = 200. This seems high for distance.
                                Assuming 'good_matches' count is the primary metric rather than distance value itself for thresholding.
                                Let's rename to sift_good_match_min_count for clarity if original intent was minimum number of matches.
                                Original code: good_matches = [m for m in matches if m.distance < Threshold]
                                This threshold is actually for the *distance* of individual matches.
                                The number of such "good_matches" is then compared.
                                The name THRESHOLD_PATTERN_MATCH seems to imply a threshold on the *number* of good matches,
                                but its value (e.g., 200) was used as a distance cutoff.
                                Let's assume `threshold_match_val` is this distance cutoff.
    Returns:
        str: Name of the best matching pattern or None.
    """
    if test_image_cv is None:
        logger.error("patternThresholding: Input test_image is None.")
        return None
    
    test_img_gray = test_image_cv
    if len(test_image_cv.shape) == 3:
         test_img_gray = cv2.cvtColor(test_image_cv, cv2.COLOR_BGR2GRAY)


    sift = cv2.SIFT_create()
    try:
        test_keypoints, test_descriptors = sift.detectAndCompute(test_img_gray, None)
    except cv2.error as e:
        logger.error(f"SIFT error on test image: {e}")
        return None


    if test_descriptors is None or test_keypoints is None or len(test_keypoints) == 0 :
        logger.warning("No SIFT descriptors found for test image.")
        return None

    bf = cv2.BFMatcher(cv2.NORM_L2, crossCheck=True)
    best_match_name = None
    max_good_matches = 0 # We want the pattern with the most "good" matches

    for pattern_img_gray, pattern_name in loaded_pattern_images_data:
        if pattern_img_gray is None:
            logger.warning(f"Skipping None pattern image: {pattern_name}")
            continue
        try:
            pattern_keypoints, pattern_descriptors = sift.detectAndCompute(pattern_img_gray, None)
        except cv2.error as e:
            logger.error(f"SIFT error on pattern image {pattern_name}: {e}")
            continue


        if pattern_descriptors is None or pattern_keypoints is None or len(pattern_keypoints) == 0:
            logger.warning(f"No SIFT descriptors found for pattern: {pattern_name}")
            continue

        try:
            matches = bf.match(test_descriptors, pattern_descriptors)
            matches = sorted(matches, key=lambda x: x.distance)
            # `threshold_match_val` is the distance cutoff for a match to be "good"
            good_matches = [m for m in matches if m.distance < threshold_match_val]
            num_good_matches = len(good_matches)

            if num_good_matches > max_good_matches:
                max_good_matches = num_good_matches
                best_match_name = pattern_name
        except cv2.error as e:
            logger.error(f"Error during SIFT matching for pattern {pattern_name}: {e}")
            continue
            
    # Decision: what is a "match"? If any pattern has at least X good_matches?
    # The original `THRESHOLD_PATTERN_MATCH` might have been intended as min number of good matches.
    # For now, returning the name of pattern with most good matches.
    # If a minimum number of good matches is required to consider it a "match" at all,
    # that would be another threshold. Let's assume `max_good_matches > 0` is enough.
    # The variable `THRESHOLD_PATTERN_MATCH` from the original code, set to 200, was used as `m.distance < Threshold`.
    # This implies it was a distance threshold.

    return best_match_name if max_good_matches > 0 else "No_Pattern_Match"


def sort_into_folders(output_base_dir, name_folder, raw_image_cv, raw_image_name, realsense_image_cv, realsense_image_name):
    """Sorts images into named subfolders under the output base directory."""
    target_dir_raw = Path(output_base_dir) / name_folder / "raw"
    target_dir_realsense = Path(output_base_dir) / name_folder / "realsense"

    target_dir_raw.mkdir(parents=True, exist_ok=True)
    target_dir_realsense.mkdir(parents=True, exist_ok=True)

    cv2.imwrite(str(target_dir_raw / raw_image_name), raw_image_cv)
    cv2.imwrite(str(target_dir_realsense / realsense_image_name), realsense_image_cv)

def create_text_file_with_removed_images(output_base_dir, removed_images_log):
    """Creates a log file for removed images."""
    log_path = Path(output_base_dir) / "removed_images_log.txt"
    with open(log_path, "w") as f:
        f.write("Log of images removed or not fitting criteria:\n")
        for file_name, reason in removed_images_log:
            f.write(f"Frame/Image name: {file_name}, Reason: {reason}\n")

def create_zip_from_directory(directory_path_str, job_id):
    """Creates a zip file from a directory, named with job_id."""
    dir_path = Path(directory_path_str)
    # Place zip in a general downloads area, not inside the dir_path itself
    zip_filename = f"Results_{job_id}.zip"
    zip_path = Path("downloads") / zip_filename # Ensure 'downloads' dir exists at main.py level
    
    Path("downloads").mkdir(exist_ok=True)

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(dir_path):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(dir_path)
                zipf.write(file_path, arcname)
    logger.info(f"ZIP file created at: {zip_path}")
    return zip_filename # Return only the name for URL construction


def load_and_process_frame_pair(
    raw_frame_cv, raw_frame_name, realsense_frame_cv, realsense_frame_name,
    loaded_pattern_images_data, # List of (pattern_cv_gray, pattern_name)
    output_base_dir_for_accepted,
    # Configurable parameters
    run_solid_color_check: bool,
    run_object_detection: bool,
    run_pattern_matching: bool,
    # Thresholds and prompts
    bw_filter_params: dict, # {'black_thresh', 'white_thresh', 'percentage_thresh'}
    obj_detect_prompt: str,
    pattern_match_sift_distance_thresh: int
):
    """
    Processes a single pair of raw and realsense frames based on active pipeline stages.
    Returns: (bool_accepted, reason_or_category_name)
    """
    # 1. Solid Color Check (on RealSense frame)
    if run_solid_color_check:
        if is_mostly_black_or_white(realsense_frame_cv,
                                    bw_filter_params['black_thresh'],
                                    bw_filter_params['white_thresh'],
                                    bw_filter_params['percentage_thresh']):
            logger.info(f"{realsense_frame_name}: Rejected by solid color check.")
            return False, "Rejected: Mostly black or white"

    # 2. Model Object Detection (on Raw frame)
    if run_object_detection:
        objects_detected, model_reason = modelObjectDetection(raw_frame_cv, obj_detect_prompt)
        if objects_detected: # True if man-made objects are detected
            logger.info(f"{raw_frame_name}: Rejected by object detection. Reason: {model_reason}")
            return False, f"Rejected: Man-made objects detected ({model_reason[:50]}...)"

    # 3. Pattern Matching (on RealSense frame)
    classification_name = "Uncategorized" # Default if pattern matching is off or no match
    if run_pattern_matching:
        if not loaded_pattern_images_data:
            logger.warning(f"{realsense_frame_name}: Pattern matching is ON but no pattern images were loaded/provided.")
            classification_name = "No_Patterns_Available"
        else:
            best_match_name = patternThresholding(realsense_frame_cv, loaded_pattern_images_data, pattern_match_sift_distance_thresh)
            if best_match_name:
                classification_name = best_match_name
                logger.info(f"{realsense_frame_name}: Matched pattern '{best_match_name}'.")
            else: # No pattern met criteria
                classification_name = "No_Pattern_Match"
                logger.info(f"{realsense_frame_name}: No suitable pattern match found.")
                # Depending on requirements, "No_Pattern_Match" might be a rejection or a category.
                # For now, let's assume it's a category. If it's a rejection:
                # return False, "Rejected: No pattern match"

    # If all checks passed (or were skipped), sort the image
    sort_into_folders(output_base_dir_for_accepted, classification_name, raw_frame_cv, raw_frame_name, realsense_frame_cv, realsense_frame_name)
    return True, classification_name


def process_video_frames(
    job_id: str, # For unique output folder
    path_to_raw_video: str,
    path_to_realsense_video: str,
    pattern_image_paths: list, # List of full paths to pattern images
    # Configurable parameters from main.py session
    thres_params: dict, # Contains all numerical thresholds and text prompts
    pipeline_processes_config: dict # Booleans for active stages
):
    """
    Extracts frames from videos, processes them according to pipeline_processes_config and thres_params.
    Saves accepted images into categorized folders and creates a ZIP archive.
    Returns: (number_of_frames_processed, name_of_output_zip_file)
    """
    logger.info(f"Job {job_id}: Starting video processing. Raw: '{path_to_raw_video}', RealSense: '{path_to_realsense_video}'")
    
    # Define base output directory for this job's accepted images
    # This should be unique per job to avoid conflicts if jobs run concurrently or use same session data
    # And cleaned up if needed. Let's make it job_id specific.
    # e.g., frames_output/session_xyz/job_abc/Accepted_images/
    # For simplicity, let's assume a top-level 'pipeline_output' dir, then job_id.
    # This 'pipeline_output' dir is at the same level as 'uploads', 'downloads'.
    
    PIPELINE_OUTPUT_ROOT = Path("pipeline_output")
    PIPELINE_OUTPUT_ROOT.mkdir(exist_ok=True)
    output_base_dir = PIPELINE_OUTPUT_ROOT / job_id / "Accepted_images"
    if output_base_dir.exists():
        shutil.rmtree(output_base_dir)
    output_base_dir.mkdir(parents=True, exist_ok=True)

    # Load pattern images
    loaded_patterns = []
    if pipeline_processes_config.get('Pattern Thresholding', False) and pattern_image_paths:
        logger.info(f"Job {job_id}: Loading {len(pattern_image_paths)} pattern images.")
        for img_path_str in pattern_image_paths:
            try:
                img_path = Path(img_path_str)
                pattern_cv_img = cv2.imread(str(img_path))
                if pattern_cv_img is not None:
                    # Convert to grayscale for SIFT consistency
                    pattern_cv_gray = cv2.cvtColor(pattern_cv_img, cv2.COLOR_BGR2GRAY)
                    loaded_patterns.append((pattern_cv_gray, img_path.name))
                else:
                    logger.warning(f"Job {job_id}: Could not load pattern image: {img_path_str}")
            except Exception as e:
                 logger.error(f"Job {job_id}: Error loading pattern image {img_path_str}: {e}")
    elif pipeline_processes_config.get('Pattern Thresholding', False):
        logger.warning(f"Job {job_id}: Pattern Thresholding is ON, but no pattern image paths were provided.")


    cap_raw = cv2.VideoCapture(path_to_raw_video)
    cap_realsense = cv2.VideoCapture(path_to_realsense_video)

    if not cap_raw.isOpened():
        logger.error(f"Job {job_id}: Error: Unable to open raw video: {path_to_raw_video}")
        raise IOError(f"Could not open raw video: {path_to_raw_video}")
    if not cap_realsense.isOpened():
        logger.error(f"Job {job_id}: Error: Unable to open RealSense video: {path_to_realsense_video}")
        cap_raw.release() # Release the already opened one
        raise IOError(f"Could not open RealSense video: {path_to_realsense_video}")

    frame_count = 0
    processed_frame_count = 0
    removed_images_log_data = []

    # Prepare parameters for load_and_process_frame_pair
    bw_params = {
        'black_thresh': thres_params.get('Black Threshold BW', 30),
        'white_thresh': thres_params.get('White Threshold BW', 225),
        'percentage_thresh': thres_params.get('Solid Color Detection', 0.60) # This was THRESHOLD_BLACK_WHITE
    }
    obj_det_prompt = thres_params.get('Object Detection Prompt', "Analyze...")
    # This was THRESHOLD_PATTERN_MATCH, used as a distance.
    sift_distance_thresh = thres_params.get('Pattern Thresholding Value', 200)


    while True:
        ret_raw, raw_frame = cap_raw.read()
        ret_realsense, realsense_frame = cap_realsense.read()

        if not ret_raw or not ret_realsense:
            logger.info(f"Job {job_id}: Reached end of one or both videos after {frame_count} iterations.")
            break

        raw_frame_name = f"raw_frame_{frame_count:05d}.png" # Save as png for quality
        realsense_frame_name = f"realsense_frame_{frame_count:05d}.png"

        accepted, reason_or_category = load_and_process_frame_pair(
            raw_frame, raw_frame_name, realsense_frame, realsense_frame_name,
            loaded_patterns,
            output_base_dir, # Pass the specific output dir for accepted images
            run_solid_color_check=pipeline_processes_config.get('Solid Color Detection', True),
            run_object_detection=pipeline_processes_config.get('Model Object Detection', True),
            run_pattern_matching=pipeline_processes_config.get('Pattern Thresholding', True),
            bw_filter_params=bw_params,
            obj_detect_prompt=obj_det_prompt,
            pattern_match_sift_distance_thresh=sift_distance_thresh
        )

        if not accepted:
            removed_images_log_data.append((raw_frame_name, reason_or_category))
        
        processed_frame_count +=1
        if frame_count % 100 == 0: # Log progress
            logger.info(f"Job {job_id}: Processed {frame_count} frame pairs...")
        frame_count +=1


    cap_raw.release()
    cap_realsense.release()
    logger.info(f"Job {job_id}: Finished processing video frames. Total pairs iterated: {frame_count}, successfully processed: {processed_frame_count - len(removed_images_log_data)}")

    create_text_file_with_removed_images(output_base_dir, removed_images_log_data)
    
    # Zip the contents of output_base_dir
    if os.listdir(output_base_dir): # Only zip if there's content
        zip_file_name = create_zip_from_directory(str(output_base_dir), job_id)
        logger.info(f"Job {job_id}: Successfully created ZIP file: {zip_file_name}")
    else:
        logger.info(f"Job {job_id}: No images were accepted. ZIP file not created.")
        zip_file_name = None # Or an empty zip, depending on requirements

    # Optionally, clean up the unzipped output_base_dir after zipping
    # shutil.rmtree(output_base_dir)
    # logger.info(f"Job {job_id}: Cleaned up temporary processing directory: {output_base_dir}")


    return processed_frame_count, zip_file_name



# import ollama
# import cv2
# import numpy as np
# import re
# import os
# import zipfile
# import io
# import shutil
# import time

# PATTERN_DIR = "../pattern"  # Directory containing pattern images
# PATTERN_IMAGES = []
# PATTERN_NAMES = []

# THRESHOLD_BLACK_WHITE = 0.60  # Threshold for black or white percentage
# THRESHOLD_PATTERN_MATCH = 200  # Threshold for pattern matching


# def is_mostly_black_or_white(image, threshold=THRESHOLD_BLACK_WHITE):
#     """
#     Check if an image is mostly black or white.

#     Args:
#         image (str): Path to the image to be checked
#         threshold (float): Threshold for black or white percentage

#     Returns:
#         bool: True if the image is mostly black or white, False otherwise

#     """
#     if len(image.shape) == 3:
#         image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

#     total_pixels = image.size

#     black_threshold = 30
#     white_threshold = 225

#     black_pixels = np.sum(image <= black_threshold)
#     white_pixels = np.sum(image >= white_threshold)

#     black_percentage = black_pixels / total_pixels
#     white_percentage = white_pixels / total_pixels

#     return black_percentage >= threshold or white_percentage >= threshold

# def modelObjectDetection(frame):
#     """
#     Detect man-made objects in an image using a pre-trained model fro Ollama.

#     Args:
#         frame (str): Path to the image to be checked

#     Returns:
#         bool : True if there is a man-made object in the image, False otherwise

#     """

#     objects_detected = True #True if man-made objects are detected, False if natural objects are detected

#     _, buffer = cv2.imencode('.jpg', frame)
#     image_bytes = buffer.tobytes()

#     res = ollama.chat(
#         model="llava:34b",
#         messages=[
#             {
#                 'role': 'user',
#                 'content': "Analyze the image and determine with at least 70% confidence whether it contains man-made objects (buildings, houses, light poles, cars, sheds, or artificial structures) that affect depth; exclude natural elements like trees or paths in mostly tree-covered images, and explicitly state 'True' or 'False' before listing identified objects or explaining uncertainty.",
#                 'images': [image_bytes]  # Pass bytes instead of a NumPy array
#             }
#         ]
#     )


#     if re.search(r'False', res['message']['content']):
#         objects_detected = False

#     return objects_detected, res['message']['content']


# def patternThresholding(test_image):
#     """
#     Compare an image against multiple patterns using SIFT features and return the best match.

#     Args:
#         test_image (Ndarray): image to be matched
#         pattern_imgs (list): List of pattern image arrays
#         pattern_names (list): List of pattern image names

#     Returns:
#         tuple: (best_matching_pattern_name, best_matching_pattern_img, number_of_good_matches)
#     """
#     Threshold = THRESHOLD_PATTERN_MATCH  # You can adjust this threshold

#     # Initialize SIFT detector
#     sift = cv2.SIFT_create()

#     # Load and process test image
#     test_img = test_image


#     if test_img is None:
#         raise ValueError(f"Could not load test image: {test_image}")

#     # Get keypoints and descriptors for test image
#     test_keypoints, test_descriptors = sift.detectAndCompute(test_img, None)

#     # Initialize matcher
#     bf = cv2.BFMatcher(cv2.NORM_L2, crossCheck=True)

#     best_match_img = None
#     best_match_name = None
#     best_match_count = 0

#     # Compare with each pattern
#     for pattern_img, pattern_name in zip(PATTERN_IMAGES, PATTERN_NAMES):
#         if pattern_img is None:
#             print(f"Warning: Could not load pattern: {pattern_name}")
#             continue

#         if len(pattern_img.shape) == 3:
#             pattern_img_gray = cv2.cvtColor(pattern_img, cv2.COLOR_BGR2GRAY)
#         else:
#             pattern_img_gray = pattern_img

#         # Get keypoints and descriptors for pattern
#         pattern_keypoints, pattern_descriptors = sift.detectAndCompute(pattern_img_gray, None)

#         if pattern_descriptors is None or test_descriptors is None:
#             continue

#         # Match descriptors
#         matches = bf.match(test_descriptors, pattern_descriptors)

#         matches = sorted(matches, key=lambda x: x.distance)

#         # Count good matches (you can adjust the threshold)
#         good_matches = [m for m in matches if m.distance < Threshold]
#         num_good_matches = len(good_matches)

#         # Update best match if this pattern has more good matches
#         if num_good_matches > best_match_count:
#             best_match_count = num_good_matches
#             best_match_img = pattern_img
#             best_match_name = pattern_name

#     return best_match_name

# def sort_into_folders(root_folder,name_folder, raw_image,raw_image_name, realsense_image, realsense_image_name):
#     """
#     Sort images into folders based on the name of the pattern.

#     Args:
#         root_folder (str): Path to the root folder
#         name_folder (str): Name of the folder to be created
#         raw_image (numpy array): Raw image
#         raw_image_name (str): Name of the raw image
#         realsense_image (numpy array): Realsense image
#         realsense_image_name (str): Name of the realsense image


#     """


#     if not os.path.exists(os.path.join(root_folder, name_folder, "raw")):
#         os.makedirs(os.path.join(root_folder, name_folder, "raw"))

#     if not os.path.exists(os.path.join(root_folder, name_folder, "realsense")):
#         os.makedirs(os.path.join(root_folder, name_folder, "realsense"))

#     cv2.imwrite(os.path.join(root_folder, name_folder, "raw", raw_image_name), raw_image)
#     cv2.imwrite(os.path.join(root_folder, name_folder, "realsense", realsense_image_name), realsense_image)


# def create_text_file_with_removed_images(root_folder, removed_images):
#     """
#     Create a text file with the names of the removed images in the root folder.

#     Args:
#         root_folder (str): Path to the root folder
#         removed_images_raw (list): List of removed raw images
#         removed_images_realsense (list): List of removed realsense images

#     """
#     with open(os.path.join(root_folder, "removed_images.txt"), "w") as f:
#         f.write("Images removed because they are mostly black or white or contain natural objects\n")
#         for file_name, reason in removed_images:
#             f.write(f"frame image: {file_name}, Reason: {reason}\n")
#         f.close()

# def create_zip_from_directory(directory_path, zip_filename="Results.zip"):
#     """
#     Create a zip file from a directory.

#     Args:
#         directory_path (str): Path to the directory to be zipped
#         zip_filename (str): Name of the zip file
#     """

#     memory_zip = io.BytesIO()

#     # Create zip file in memory
#     with zipfile.ZipFile(memory_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
#         for root, dirs, files in os.walk(directory_path):
#             for file in files:
#                 file_path = os.path.join(root, file)
#                 arcname = os.path.relpath(file_path, directory_path)
#                 zipf.write(file_path, arcname)


#     memory_zip.seek(0)
#     script_dir = os.getcwd()
#     zip_path = os.path.join(script_dir, zip_filename)

#     with open(zip_path, "wb") as f:
#         f.write(memory_zip.getvalue())

#     print(f"ZIP file created at: {zip_path}")

#     return zip_path




# def load_images(raw_image,raw_image_name, realsense_image, realsense_image_name, dir_pattern = PATTERN_DIR):
#     """
#     Load images from the specified directories and process them.

#     Args:
#         raw_image (numpy): the raw image
#         raw_image_name (str): Name of the raw image
#         realsense_image (numpy array): the realsense image
#         realsense_image_name (str): Name of the realsense image
#         dir_pattern (str): Path to the directory containing pattern images

#     """
#     if not is_mostly_black_or_white(realsense_image):
#         object_detected, reason = modelObjectDetection(raw_image)
#         if not object_detected:
#             name = patternThresholding(realsense_image)
#             print("Accepted_images", name, raw_image_name)
#             sort_into_folders("Accepted_images", name, raw_image, raw_image_name, realsense_image, realsense_image_name)
#             return True, name
#         else:
#             print(reason)
#             return False, reason

#     else:
#         print("Image is mostly black or white")
#         return False, "Image is mostly black or white"


# def videos_to_frames(path_to_raw_video, path_to_realsense_video):
#     """
#     Extract frames from two videos and process them.

#     Args:
#         path_to_video_one (str): Path to the first video
#         path_to_video_two (str): Path to the second video
#     """
#     remove_images = []
#     global PATTERN_IMAGES, PATTERN_NAMES, PATTERN_DIR
#     for f in os.listdir(PATTERN_DIR):
#         if f.endswith('.png'):
#             img = cv2.imread(os.path.join(PATTERN_DIR, f))
#             if img is not None:
#                 PATTERN_IMAGES.append(img)
#                 PATTERN_NAMES.append(f)

#     root_folder = "Accepted_images"
#     if not os.path.exists(root_folder):
#         os.makedirs(root_folder)
#     else:
#         shutil.rmtree(root_folder) #remove the folder and its contents and create a new one
#         os.makedirs(root_folder)

#     cap_raw_video = cv2.VideoCapture(path_to_raw_video)
#     cap_realsense_video = cv2.VideoCapture(path_to_realsense_video)
    
#     if not cap_raw_video.isOpened() or not cap_realsense_video.isOpened():
#         print("Error: Unable to open video.")

#     frame_count = 0

#     while True:
#         ret1, frame1 = cap_raw_video.read() #raw frame
#         ret2, frame2 = cap_realsense_video.read() #realsense frame
#         #once on of the videos reaches the end, break the loop
#         if not ret1 or not ret2:
#             break

#         frame_filename1 = f"frame_{frame_count:05d}.jpg"
#         frame_filename2 = f"frame_{frame_count:05d}.jpg"


#         is_it_a_good_image, reason = load_images(frame1, frame_filename1, frame2, frame_filename2)
#         if not is_it_a_good_image:
#             remove_images.append((frame_filename1, reason))

#         frame_count += 1

#     cap_raw_video.release()
#     cap_realsense_video.release()

#     create_text_file_with_removed_images("Accepted_images", remove_images)

#     return frame_count

# #edit so it gets passed the path to the video
# def main():
#     start = time.time()
#     realsense_video_path = "videos/realsense_006.mp4"
#     raw_video_path = "videos/raw_006.mp4"

#     frame_count = videos_to_frames(raw_video_path, realsense_video_path)

#     print(f"Processed {frame_count} frames")

#     zip_dir = "Accepted_images"
#     zip_file = create_zip_from_directory(zip_dir)

#     end = time.time()
#     print(end - start)
#     print("Pipeline completed successfully!")
#     print("You can download the output zip file from the following link:", zip_file)


# if __name__ == "__main__":
#     main()