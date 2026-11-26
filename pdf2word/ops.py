"""Python bindings for Rust core operators."""

try:
    from pdf2word_ops import (
        coords_px_to_pt,
        coords_pt_to_emu,
        sort_table_cells,
        sort_layout_blocks,
        nms_boxes,
        merge_text_lines,
        compute_fit_font_size,
    )
except ImportError:
    # Fallback pure-Python implementations for development without Rust build
    import numpy as np
    from typing import List, Optional

    def coords_px_to_pt(arr: np.ndarray, dpi: float) -> np.ndarray:
        """Convert pixel coordinates to points (1/72 inch)."""
        return (arr.astype(np.float32) * (72.0 / dpi)).astype(np.float32)

    def coords_pt_to_emu(arr: np.ndarray) -> np.ndarray:
        """Convert point coordinates to EMU (1/914400 inch)."""
        return np.round(arr.astype(np.float32) * 12700.0).astype(np.int64)

    def sort_table_cells(cells: np.ndarray) -> List[int]:
        """Sort table cells in reading order (row-major)."""
        if len(cells) == 0:
            return []
        # Sort by row_id (col 3), col_id (col 2), y1 (col 1), x1 (col 0)
        # lexsort sorts by last key first, so reverse the desired sort order
        idx = np.lexsort((cells[:, 1], cells[:, 0], cells[:, 3], cells[:, 2]))
        return idx.tolist()

    def sort_layout_blocks(
        bboxes: np.ndarray,
        block_types: np.ndarray,
        img_width: float,
        img_height: float,
    ) -> List[int]:
        """Simple top-left to bottom-right sorting fallback."""
        if len(bboxes) == 0:
            return []
        # Sort by y1, then x1
        idx = np.lexsort((bboxes[:, 0], bboxes[:, 1]))
        return idx.tolist()

    def nms_boxes(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> List[int]:
        """Pure Python NMS fallback."""
        if len(boxes) == 0:
            return []
        
        idx = scores.argsort()[::-1]
        keep = []
        suppressed = np.zeros(len(boxes), dtype=bool)
        
        for i in idx:
            if suppressed[i]:
                continue
            keep.append(i)
            
            x1_i, y1_i, x2_i, y2_i = boxes[i]
            area_i = max(0, x2_i - x1_i) * max(0, y2_i - y1_i)
            
            for j in idx:
                if i == j or suppressed[j]:
                    continue
                x1_j, y1_j, x2_j, y2_j = boxes[j]
                area_j = max(0, x2_j - x1_j) * max(0, y2_j - y1_j)
                
                ix1 = max(x1_i, x1_j)
                iy1 = max(y1_i, y1_j)
                ix2 = min(x2_i, x2_j)
                iy2 = min(y2_i, y2_j)
                
                inter_w = max(0, ix2 - ix1)
                inter_h = max(0, iy2 - iy1)
                inter = inter_w * inter_h
                union = area_i + area_j - inter
                
                if union > 0 and inter / union > iou_threshold:
                    suppressed[j] = True
        
        return keep

    def merge_text_lines(
        boxes: np.ndarray,
        line_heights: np.ndarray,
        v_threshold_ratio: float,
    ) -> List[List[int]]:
        """Merge adjacent text lines into paragraphs."""
        if len(boxes) == 0:
            return []
        
        idx = np.argsort(boxes[:, 1])
        paragraphs = []
        current = [idx[0]]
        last_bottom = boxes[idx[0], 3]
        avg_h = line_heights[idx[0]]
        
        for i in idx[1:]:
            gap = boxes[i, 1] - last_bottom
            if gap <= avg_h * v_threshold_ratio:
                current.append(i)
                avg_h = (avg_h * (len(current) - 1) + line_heights[i]) / len(current)
            else:
                paragraphs.append(current)
                current = [i]
                avg_h = line_heights[i]
            last_bottom = boxes[i, 3]
        paragraphs.append(current)
        return paragraphs

    def compute_fit_font_size(
        text_length: int,
        bbox_width_pt: float,
        bbox_height_pt: float,
        avg_char_width_ratio: Optional[float] = None,
    ) -> float:
        """Compute font size to fit text in bbox."""
        ratio = avg_char_width_ratio or 0.5
        if text_length == 0:
            return 12.0
        
        fs = 12.0
        for _ in range(10):
            char_w = fs * ratio
            chars_per_line = max(1, bbox_width_pt / char_w)
            lines = np.ceil(text_length / chars_per_line)
            new_fs = bbox_height_pt / lines
            if abs(new_fs - fs) < 0.1:
                break
            fs = new_fs
        
        return float(np.clip(fs, 6.0, 72.0))

__all__ = [
    "coords_px_to_pt",
    "coords_pt_to_emu",
    "sort_table_cells",
    "sort_layout_blocks",
    "nms_boxes",
    "merge_text_lines",
    "compute_fit_font_size",
]