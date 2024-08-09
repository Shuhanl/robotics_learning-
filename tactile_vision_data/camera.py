import pyrealsense2 as rs
import numpy as np
import cv2
import apriltag

class RealSenseCamera:
    def __init__(self):
        self.pipeline = rs.pipeline()
        self.config = rs.config()

        # Configure the streams
        self.config.enable_stream(rs.stream.color, 640, 480, rs.format.bgr8, 30)
        self.config.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)

        # Start the pipeline
        self.pipeline.start(self.config)

        """ April tag """
        self.family="tag36h11"
        self.options = apriltag.DetectorOptions(self.family)
        self.detector = apriltag.Detector(self.options)

        self.num_calib_image = 10
        self.num_calib_tag = 1
        self.tag_size = 0.05  # AprilTag side length in meters
        self.image_size = (640, 480)
        self.camera_params = [640, 480, 320, 240]  # fx, fy, cx, cy
 
    def capture_frames(self):
        # This function captures frames from both color and depth streams
        frames = self.pipeline.wait_for_frames()
        depth_frame = frames.get_depth_frame()
        color_frame = frames.get_color_frame()

        if not depth_frame or not color_frame:
            return None, None

        depth_image = np.asanyarray(depth_frame.get_data())
        color_image = np.asanyarray(color_frame.get_data())

        # Apply color map to depth image to visualize it
        depth_colormap = cv2.applyColorMap(cv2.convertScaleAbs(depth_image, alpha=0.5), cv2.COLORMAP_JET)

        return color_image, depth_colormap
    
    def detect_apriltags(self, color_image):
        # Convert to grayscale
        gray_image = cv2.cvtColor(color_image, cv2.COLOR_BGR2GRAY)

        # Detect AprilTags in the image
        tags = self.detector.detect(gray_image)
        for tag in tags:
            # Extract the bounding box and display it
            (ptA, ptB, ptC, ptD) = tag.corners
            ptB = (int(ptB[0]), int(ptB[1]))
            ptC = (int(ptC[0]), int(ptC[1]))
            ptA = (int(ptA[0]), int(ptA[1]))
            ptD = (int(ptD[0]), int(ptD[1]))

            cv2.line(color_image, ptA, ptB, (0, 255, 0), 2)
            cv2.line(color_image, ptB, ptC, (0, 255, 0), 2)
            cv2.line(color_image, ptC, ptD, (0, 255, 0), 2)
            cv2.line(color_image, ptD, ptA, (0, 255, 0), 2)

            # Calculate the pose of the tag
            pose, e0, e1 = self.detector.detection_pose(tag, self.camera_params, self.tag_size)

            # Decompose the rotation matrix to Euler angles
            rvec, _ = cv2.Rodrigues(pose[:3, :3])
            tvec = pose[:3, 3]

            # Display the translation vector (position) and rotation vector (orientation)
            cv2.putText(color_image, f'Position: {tvec[0]:.2f}, {tvec[1]:.2f}, {tvec[2]:.2f}', (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
            cv2.putText(color_image, f'Orientation: {rvec[0,0]:.2f}, {rvec[1,0]:.2f}, {rvec[2,0]:.2f}', (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
            
            # Put the tag ID on the image
            tag_id = "ID: {}".format(tag.tag_id)
            cv2.putText(color_image, tag_id, (ptA[0], ptA[1] - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

        return tags

    def calibrate_camera(self):
        images = []
        object_points = []
        image_points = []

        try:
            while True:  # Changed to a continuous loop until sufficient tags are detected.
                frames = self.pipeline.wait_for_frames()
                color_frame = frames.get_color_frame()
                if not color_frame:
                    continue
                image = np.asanyarray(color_frame.get_data())

                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                detections = self.detector.detect(gray)
                if len(detections) >= self.num_calib_tag:  # Check if at least num_calib_tag are detected.
                    print(f"Detected {len(detections)} tags, sufficient for calibration.")
                    images.append(image)
                    for detection in detections:
                        corners = detection.corners
                        image_points.append(corners)
                        object_points.append(np.array([
                            [-self.tag_size/2, -self.tag_size/2, 0],
                            [self.tag_size/2, -self.tag_size/2, 0],
                            [self.tag_size/2, self.tag_size/2, 0],
                            [-self.tag_size/2, self.tag_size/2, 0]
                        ], dtype=np.float32))
                    if len(images) >= self.num_calib_image:  # Check if enough images are collected.
                        break
                else:
                    print(f"Detected {len(detections)} tags, need at least num_calib_tag for a valid calibration image.")

        finally:
            self.pipeline.stop()

        # Proceed with calibration if sufficient valid images have been collected.
        ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(object_points, image_points, self.image_size, None, None)
        print(f"Calibration successful: {ret}")

        return ret, mtx, dist, rvecs, tvecs

    
    def release(self):
        # Stop the pipeline
        self.pipeline.stop()


def main():
    camera = RealSenseCamera()

    ret, mtx, dist, rvecs, tvecs = camera.calibrate_camera()
    print("Calibration successful:", ret)
    print("Camera matrix:\n", mtx)
    print("Distortion coefficients:\n", dist)

    # try:
    #     while True:
    #         color_image, depth_colormap = camera.capture_frames()
    #         if color_image is None or depth_colormap is None:
    #             continue

    #         # Tag detection
    #         tags = camera.detect_apriltags(color_image)

    #         cv2.imshow('RGB with Markers', color_image)
    #         cv2.imshow('Depth', depth_colormap)

    #         if cv2.waitKey(1) & 0xFF == ord('q'):
    #             break
    # finally:
    #     camera.release()
    #     cv2.destroyAllWindows()

if __name__ == '__main__':
    main()
