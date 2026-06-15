#!/usr/bin/env python3
"""
VideoCompressor - A tool to reduce video file size while maintaining quality.
Supports all video formats that FFmpeg can process.
"""

import os
import sys
import json
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class VideoCompressor:
    """
    Compresses video files using FFmpeg with various quality presets.
    Maintains video quality while significantly reducing file size.
    """
    
    # Quality presets with recommended CRF values and encoder settings
    QUALITY_PRESETS = {
        'ultra_high': {
            'crf': 15,  # Lower CRF = better quality, larger file
            'preset': 'slow',
            'description': 'Ultra high quality, largest file size'
        },
        'high': {
            'crf': 18,
            'preset': 'medium',
            'description': 'High quality, good compression balance'
        },
        'balanced': {
            'crf': 23,  # Default CRF value
            'preset': 'medium',
            'description': 'Balanced quality and compression'
        },
        'medium': {
            'crf': 28,
            'preset': 'fast',
            'description': 'Medium quality, smaller file size'
        },
        'low': {
            'crf': 32,  # Higher CRF = lower quality, smaller file
            'preset': 'veryfast',
            'description': 'Low quality, smallest file size'
        }
    }
    
    # Supported audio codecs
    AUDIO_CODECS = {
        'aac': {'codec': 'aac', 'bitrate': '128k'},
        'mp3': {'codec': 'libmp3lame', 'bitrate': '128k'},
        'opus': {'codec': 'libopus', 'bitrate': '128k'},
        'copy': {'codec': 'copy', 'bitrate': None}  # Copy original audio
    }
    
    def __init__(self):
        """Initialize the VideoCompressor."""
        self._verify_ffmpeg()
    
    def _verify_ffmpeg(self) -> None:
        """Verify that FFmpeg is installed and accessible."""
        try:
            subprocess.run(
                ['ffmpeg', '-version'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
            logger.info("FFmpeg found and ready")
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error("FFmpeg is not installed or not in PATH")
            logger.error("Please install FFmpeg: https://ffmpeg.org/download.html")
            sys.exit(1)
    
    def get_video_info(self, video_path: str) -> Dict:
        """
        Get detailed information about the video file.
        
        Args:
            video_path: Path to the video file
            
        Returns:
            Dictionary containing video metadata
        """
        try:
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', '-show_streams', video_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        except Exception as e:
            logger.error(f"Failed to get video info: {e}")
            return {}
    
    def get_file_size(self, video_path: str) -> Tuple[float, str]:
        """
        Get the file size in human-readable format.
        
        Args:
            video_path: Path to the video file
            
        Returns:
            Tuple of (size in bytes, human-readable format)
        """
        size_bytes = os.path.getsize(video_path)
        
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024:
                return size_bytes, f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024
        
        return size_bytes, f"{size_bytes:.2f} TB"
    
    def compress_video(
        self,
        input_path: str,
        output_path: str,
        quality: str = 'balanced',
        audio_codec: str = 'aac',
        keep_original: bool = True
    ) -> bool:
        """
        Compress a video file using FFmpeg.
        
        Args:
            input_path: Path to input video
            output_path: Path to output video
            quality: Quality preset ('ultra_high', 'high', 'balanced', 'medium', 'low')
            audio_codec: Audio codec ('aac', 'mp3', 'opus', 'copy')
            keep_original: Keep original file after compression
            
        Returns:
            True if compression successful, False otherwise
        """
        # Validate inputs
        if not os.path.exists(input_path):
            logger.error(f"Input file not found: {input_path}")
            return False
        
        if quality not in self.QUALITY_PRESETS:
            logger.error(f"Invalid quality preset: {quality}")
            logger.error(f"Available presets: {', '.join(self.QUALITY_PRESETS.keys())}")
            return False
        
        if audio_codec not in self.AUDIO_CODECS:
            logger.error(f"Invalid audio codec: {audio_codec}")
            logger.error(f"Available codecs: {', '.join(self.AUDIO_CODECS.keys())}")
            return False
        
        # Check if output path exists
        if os.path.exists(output_path):
            logger.warning(f"Output file already exists: {output_path}")
            response = input("Overwrite? (y/n): ").strip().lower()
            if response != 'y':
                return False
        
        # Get original file size
        orig_size_bytes, orig_size_str = self.get_file_size(input_path)
        logger.info(f"Original file size: {orig_size_str}")
        
        # Get video info
        video_info = self.get_video_info(input_path)
        
        # Get quality preset settings
        preset_config = self.QUALITY_PRESETS[quality]
        audio_config = self.AUDIO_CODECS[audio_codec]
        
        logger.info(f"Compression preset: {quality} ({preset_config['description']})")
        logger.info(f"CRF value: {preset_config['crf']} (lower = better quality)")
        logger.info(f"Encoding speed: {preset_config['preset']}")
        
        # Build FFmpeg command
        cmd = [
            'ffmpeg',
            '-i', input_path,
            '-c:v', 'libx264',  # Use H.264 codec for wide compatibility
            '-crf', str(preset_config['crf']),  # Quality (0-51, lower is better)
            '-preset', preset_config['preset'],  # Encoding speed vs compression tradeoff
            '-c:a', audio_config['codec'],  # Audio codec
        ]
        
        # Add audio bitrate if not copying
        if audio_codec != 'copy' and audio_config['bitrate']:
            cmd.extend(['-b:a', audio_config['bitrate']])
        
        # Add additional optimization flags
        cmd.extend([
            '-movflags', '+faststart',  # Enable streaming from start
            output_path
        ])
        
        try:
            logger.info("Starting compression...")
            logger.info(f"Command: {' '.join(cmd)}")
            
            result = subprocess.run(cmd, check=True)
            
            # Check if output file was created
            if not os.path.exists(output_path):
                logger.error("Output file was not created")
                return False
            
            # Get compressed file size
            comp_size_bytes, comp_size_str = self.get_file_size(output_path)
            compression_ratio = ((orig_size_bytes - comp_size_bytes) / orig_size_bytes) * 100
            
            logger.info(f"Compression complete!")
            logger.info(f"Compressed file size: {comp_size_str}")
            logger.info(f"Compression ratio: {compression_ratio:.1f}%")
            logger.info(f"Size reduction: {(orig_size_bytes - comp_size_bytes) / (1024*1024):.2f} MB")
            
            if not keep_original:
                logger.info("Removing original file...")
                os.remove(input_path)
                logger.info("Original file removed")
            
            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Compression failed: {e}")
            # Clean up partial output file
            if os.path.exists(output_path):
                os.remove(output_path)
            return False
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            if os.path.exists(output_path):
                os.remove(output_path)
            return False

    def smart_compress(
        self,
        input_path: str,
        output_path: str,
        target_reduction: int = 70,
    ) -> Optional[Dict]:
        """
        Single-pass CRF compression that targets a specific file-size reduction
        while preserving visual quality.  Accepts ANY format FFmpeg can read.

        Uses CRF with maxrate capping to achieve the target size in one fast pass.

        Args:
            input_path:       Path to input video (any format)
            output_path:      Path to output video (.mp4)
            target_reduction: Target size reduction percentage (default: 70)

        Returns:
            dict with compression stats on success, None on failure
        """
        if not os.path.exists(input_path):
            logger.error(f"Input file not found: {input_path}")
            return None

        input_size = os.path.getsize(input_path)
        if input_size == 0:
            logger.error("Input file is empty")
            return None

        # ---- probe ----
        info = self.get_video_info(input_path)
        if not info or "format" not in info:
            logger.error("Cannot read video file")
            return None

        duration = float(info["format"].get("duration", 0))
        if duration <= 0:
            logger.error("Cannot determine video duration")
            return None

        video_stream = None
        audio_stream = None
        for s in info.get("streams", []):
            ct = s.get("codec_type")
            if ct == "video" and video_stream is None:
                video_stream = s
            elif ct == "audio" and audio_stream is None:
                audio_stream = s

        if not video_stream:
            logger.error("No video stream found in file")
            return None

        height = int(video_stream.get("height", 0))
        width = int(video_stream.get("width", 0))
        if height <= 0 or width <= 0:
            logger.error("Cannot determine video resolution")
            return None

        # ---- figure out current bitrate & target ----
        current_bps = int(input_size * 8 / duration)
        target_size = input_size * (1 - target_reduction / 100.0)
        target_total_bps = int((target_size * 8) / duration)

        audio_bps = 128_000 if audio_stream else 0
        target_video_bps = max(target_total_bps - audio_bps, 200_000)

        # ---- determine CRF from target ratio ----
        ratio = target_video_bps / max(current_bps, 1)
        if ratio >= 0.6:
            crf = 23
        elif ratio >= 0.3:
            crf = 28
        elif ratio >= 0.15:
            crf = 32
        else:
            crf = 36

        # ---- smart resolution scaling (disabled to maintain original resolution) ----
        vbr_kbps = target_video_bps / 1000
        out_h = height

        # video filter – always ensure even dimensions for libx264
        vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

        maxrate = f"{int(target_video_bps * 1.5) // 1000}k"
        bufsize = f"{target_video_bps * 2 // 1000}k"

        logger.info(
            f"Input : {width}x{height}, {duration:.1f}s, "
            f"{input_size / (1024 * 1024):.1f} MB"
        )
        logger.info(
            f"Target: {target_size / (1024 * 1024):.1f} MB "
            f"({target_reduction}% reduction), CRF {crf}, maxrate {maxrate}"
        )

        try:
            # ---- single-pass encode ----
            cmd = [
                "ffmpeg", "-y",
                "-i", input_path,
                "-map", "0:v:0",
                "-c:v", "libx264",
                "-crf", str(crf),
                "-maxrate", maxrate,
                "-bufsize", bufsize,
                "-preset", "fast",
                "-pix_fmt", "yuv420p",
                "-vf", vf,
            ]
            if audio_stream:
                cmd.extend(["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"])
            else:
                cmd.append("-an")
            cmd.extend(["-movflags", "+faststart", output_path])

            logger.info("Compressing …")
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f"Compression failed:\n{result.stderr[-1000:]}")
                if os.path.exists(output_path):
                    os.remove(output_path)
                return None

            if not os.path.exists(output_path):
                logger.error("Output file was not created")
                return None

            output_size = os.path.getsize(output_path)
            reduction = ((input_size - output_size) / input_size) * 100

            stats = {
                "input_size": input_size,
                "output_size": output_size,
                "reduction_percent": round(reduction, 1),
                "input_size_mb": round(input_size / (1024 * 1024), 2),
                "output_size_mb": round(output_size / (1024 * 1024), 2),
            }
            logger.info(
                f"Done! {stats['input_size_mb']} MB → {stats['output_size_mb']} MB "
                f"({stats['reduction_percent']}% reduction)"
            )
            return stats

        except Exception as e:
            logger.error(f"Compression error: {e}")
            if os.path.exists(output_path):
                os.remove(output_path)
            return None

    def custom_compress(
        self,
        input_path: str,
        output_path: str,
        mode: str = 'target_reduction',  # 'target_reduction' or 'preset'
        target_reduction: int = 60,
        quality: str = 'balanced',
        codec: str = 'h264',
        resolution: str = 'original',
        audio_codec: str = 'aac',
        preset: str = 'fast',
        hw_accel: bool = False,
        progress_callback = None
    ) -> Optional[Dict]:
        """
        Compresses a video file with advanced customizable settings and live progress feedback.
        Supports hardware acceleration, H.264/H.265 codecs, resolution scaling, and audio configuration.
        """
        if not os.path.exists(input_path):
            logger.error(f"Input file not found: {input_path}")
            return None

        input_size = os.path.getsize(input_path)
        if input_size == 0:
            logger.error("Input file is empty")
            return None

        # Probing video information
        info = self.get_video_info(input_path)
        if not info or "format" not in info:
            logger.error("Cannot read video file metadata")
            return None

        duration = float(info["format"].get("duration", 0))
        if duration <= 0:
            logger.error("Cannot determine video duration")
            return None

        video_stream = None
        audio_stream = None
        for s in info.get("streams", []):
            ct = s.get("codec_type")
            if ct == "video" and video_stream is None:
                video_stream = s
            elif ct == "audio" and audio_stream is None:
                audio_stream = s

        if not video_stream:
            logger.error("No video stream found in file")
            return None

        height = int(video_stream.get("height", 0))
        width = int(video_stream.get("width", 0))
        if height <= 0 or width <= 0:
            logger.error("Cannot determine video resolution")
            return None

        # Bitrate calculations
        current_bps = int(input_size * 8 / duration)
        target_size = input_size * (1 - target_reduction / 100.0)
        target_total_bps = int((target_size * 8) / duration)

        audio_bps = 128_000 if (audio_stream and audio_codec != 'none') else 0
        target_video_bps = max(target_total_bps - audio_bps, 200_000)

        # Map preset quality to CRF if software, or target_video_bps to CRF
        if mode == 'target_reduction':
            ratio = target_video_bps / max(current_bps, 1)
            if ratio >= 0.6:
                crf = 23
            elif ratio >= 0.3:
                crf = 28
            elif ratio >= 0.15:
                crf = 32
            else:
                crf = 36
        else:
            preset_config = self.QUALITY_PRESETS.get(quality, self.QUALITY_PRESETS['balanced'])
            crf = preset_config['crf']
            # If using preset, adapt CRF slightly for H.265 (better quality per unit CRF)
            if codec == 'h265':
                crf = min(crf + 4, 51)

        # Video compression flags
        cmd_video = []
        if hw_accel:
            # macOS Apple Silicon / Intel Hardware Acceleration
            if codec == 'h265':
                cmd_video.extend(['-c:v', 'hevc_videotoolbox'])
            else:
                cmd_video.extend(['-c:v', 'h264_videotoolbox'])

            if mode == 'target_reduction':
                cmd_video.extend(['-b:v', f"{int(target_video_bps) // 1000}k"])
            else:
                # Map quality to videotoolbox quality settings (1-100)
                q_map = {
                    'ultra_high': '75',
                    'high': '65',
                    'balanced': '55',
                    'medium': '45',
                    'low': '35'
                }
                cmd_video.extend(['-q:v', q_map.get(quality, '55')])
        else:
            # Software encoding
            if codec == 'h265':
                cmd_video.extend(['-c:v', 'libx265', '-crf', str(crf)])
            else:
                cmd_video.extend(['-c:v', 'libx264', '-crf', str(crf)])

            # Keep target reduction bounds
            if mode == 'target_reduction':
                maxrate = f"{int(target_video_bps * 1.5) // 1000}k"
                bufsize = f"{target_video_bps * 2 // 1000}k"
                cmd_video.extend(['-maxrate', maxrate, '-bufsize', bufsize])

            # Speed preset (only software encoders support speed presets directly)
            cmd_video.extend(['-preset', preset])

        # Resolution scaling
        if resolution != 'original':
            try:
                target_h = int(resolution.replace('p', ''))
                scale_factor = target_h / height if target_h < height else 1.0
                if scale_factor < 1.0:
                    out_w = int(round(width * scale_factor / 2) * 2)
                    out_h = int(round(height * scale_factor / 2) * 2)
                    vf = f"scale={out_w}:{out_h}"
                else:
                    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
            except Exception:
                vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        else:
            vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

        cmd_video.extend(['-vf', vf])

        # Audio configuration
        cmd_audio = []
        if not audio_stream or audio_codec == 'none':
            cmd_audio.extend(['-an'])
        elif audio_codec == 'copy':
            cmd_audio.extend(['-c:a', 'copy'])
        else:
            audio_encoders = {
                'aac': 'aac',
                'mp3': 'libmp3lame',
                'opus': 'libopus'
            }
            cmd_audio.extend(['-c:a', audio_encoders.get(audio_codec, 'aac'), '-b:a', '128k'])

        # Build final command
        cmd = [
            'ffmpeg', '-y',
            '-progress', '-',
            '-i', input_path,
            '-map', '0:v:0'
        ]
        if audio_stream and audio_codec != 'none':
            cmd.extend(['-map', '0:a:0'])

        cmd.extend(cmd_video)
        cmd.extend(cmd_audio)
        cmd.extend([
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            output_path
        ])

        try:
            logger.info(f"Running custom compress: {' '.join(cmd)}")
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )

            speed = "1.0x"
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=")[1])
                        elapsed_seconds = us / 1_000_000.0
                        percent = min(round((elapsed_seconds / duration) * 100, 1), 99.9)
                        if progress_callback:
                            progress_callback(percent, speed)
                    except ValueError:
                        pass
                elif line.startswith("speed="):
                    speed = line.split("=")[1].strip()

            process.wait()
            if process.returncode != 0:
                stderr_output = process.stderr.read()
                logger.error(f"FFmpeg error code {process.returncode}:\n{stderr_output[-1000:]}")
                if os.path.exists(output_path):
                    os.remove(output_path)
                return None

            if not os.path.exists(output_path):
                logger.error("Compressed output file was not found")
                return None

            output_size = os.path.getsize(output_path)
            reduction = ((input_size - output_size) / input_size) * 100

            stats = {
                "input_size": input_size,
                "output_size": output_size,
                "reduction_percent": round(reduction, 1),
                "input_size_mb": round(input_size / (1024 * 1024), 2),
                "output_size_mb": round(output_size / (1024 * 1024), 2),
            }

            if progress_callback:
                progress_callback(100.0, speed)

            return stats

        except Exception as e:
            logger.error(f"Failed to run custom compression subprocess: {e}")
            if os.path.exists(output_path):
                os.remove(output_path)
            return None


    def batch_compress(
        self,
        input_dir: str,
        output_dir: str,
        quality: str = 'balanced',
        audio_codec: str = 'aac',
        extensions: list = None
    ) -> int:
        """
        Compress all video files in a directory.
        
        Args:
            input_dir: Input directory containing videos
            output_dir: Output directory for compressed videos
            quality: Quality preset
            audio_codec: Audio codec
            extensions: Video file extensions to process (default: common formats)
            
        Returns:
            Number of successfully compressed files
        """
        if extensions is None:
            extensions = ['.mp4', '.avi', '.mkv', '.mov', '.flv', '.wmv', '.webm']
        
        if not os.path.exists(input_dir):
            logger.error(f"Input directory not found: {input_dir}")
            return 0
        
        # Create output directory
        os.makedirs(output_dir, exist_ok=True)
        
        # Find all video files
        video_files = []
        for ext in extensions:
            video_files.extend(Path(input_dir).glob(f"*{ext}"))
            video_files.extend(Path(input_dir).glob(f"*{ext.upper()}"))
        
        if not video_files:
            logger.warning(f"No video files found in {input_dir}")
            return 0
        
        logger.info(f"Found {len(video_files)} video files to process")
        
        successful = 0
        for i, video_file in enumerate(video_files, 1):
            logger.info(f"\n[{i}/{len(video_files)}] Processing: {video_file.name}")
            
            output_path = os.path.join(
                output_dir,
                f"{video_file.stem}_compressed.mp4"
            )
            
            if self.compress_video(
                str(video_file),
                output_path,
                quality=quality,
                audio_codec=audio_codec
            ):
                successful += 1
        
        logger.info(f"\n✓ Successfully compressed {successful}/{len(video_files)} files")
        return successful


def main():
    """Main entry point for the video compressor."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='VideoCompressor - Reduce video file size while maintaining quality',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Compress a single video with balanced quality
  python video_compressor.py compress video.mp4 video_compressed.mp4
  
  # Compress with high quality (larger file, better quality)
  python video_compressor.py compress video.mp4 video_compressed.mp4 --quality high
  
  # Compress with custom audio codec
  python video_compressor.py compress video.mp4 video_compressed.mp4 --audio mp3
  
  # Batch compress all videos in a directory
  python video_compressor.py batch ./videos ./compressed_videos
  
  # Show video information
  python video_compressor.py info video.mp4
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Compress command
    compress_parser = subparsers.add_parser('compress', help='Compress a single video')
    compress_parser.add_argument('input', help='Input video file path')
    compress_parser.add_argument('output', help='Output video file path')
    compress_parser.add_argument(
        '--quality',
        choices=['ultra_high', 'high', 'balanced', 'medium', 'low'],
        default='balanced',
        help='Quality preset (default: balanced)'
    )
    compress_parser.add_argument(
        '--audio',
        choices=['aac', 'mp3', 'opus', 'copy'],
        default='aac',
        help='Audio codec (default: aac)'
    )
    compress_parser.add_argument(
        '--keep-original',
        action='store_true',
        default=True,
        help='Keep original file (default: True)'
    )
    
    # Batch command
    batch_parser = subparsers.add_parser('batch', help='Batch compress videos in a directory')
    batch_parser.add_argument('input_dir', help='Input directory containing videos')
    batch_parser.add_argument('output_dir', help='Output directory for compressed videos')
    batch_parser.add_argument(
        '--quality',
        choices=['ultra_high', 'high', 'balanced', 'medium', 'low'],
        default='balanced',
        help='Quality preset (default: balanced)'
    )
    batch_parser.add_argument(
        '--audio',
        choices=['aac', 'mp3', 'opus', 'copy'],
        default='aac',
        help='Audio codec (default: aac)'
    )
    
    # Info command
    info_parser = subparsers.add_parser('info', help='Show video information')
    info_parser.add_argument('video', help='Video file path')
    
    # Presets command
    presets_parser = subparsers.add_parser('presets', help='Show available quality presets')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    compressor = VideoCompressor()
    
    if args.command == 'compress':
        success = compressor.compress_video(
            args.input,
            args.output,
            quality=args.quality,
            audio_codec=args.audio,
            keep_original=args.keep_original
        )
        sys.exit(0 if success else 1)
    
    elif args.command == 'batch':
        compressor.batch_compress(
            args.input_dir,
            args.output_dir,
            quality=args.quality,
            audio_codec=args.audio
        )
    
    elif args.command == 'info':
        info = compressor.get_video_info(args.video)
        file_size = compressor.get_file_size(args.video)
        
        logger.info(f"\nVideo Information: {args.video}")
        logger.info(f"File size: {file_size[1]}")
        logger.info(json.dumps(info, indent=2))
    
    elif args.command == 'presets':
        logger.info("\nAvailable Quality Presets:")
        logger.info("-" * 60)
        for preset_name, preset_config in compressor.QUALITY_PRESETS.items():
            logger.info(f"\n{preset_name.upper()}")
            logger.info(f"  Description: {preset_config['description']}")
            logger.info(f"  CRF: {preset_config['crf']} (0-51, lower = better quality)")
            logger.info(f"  Encoding Speed: {preset_config['preset']}")


if __name__ == '__main__':
    main()
