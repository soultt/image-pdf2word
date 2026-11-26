use pyo3::prelude::*;
use pyo3::exceptions::PyValueError;
use pyo3::wrap_pyfunction;
use numpy::{PyArray, PyArrayMethods, PyUntypedArrayMethods, IntoPyArray};
use ndarray::{ArrayView2, ArrayView1, Axis, Array2, Array1, Ix1, Ix2};
use rayon::prelude::*;
use petgraph::graph::{NodeIndex, DiGraph};
use petgraph::algo::toposort;
use std::cmp::Ordering;
use thiserror::Error;

#[derive(Error, Debug)]
pub enum OpsError {
    #[error("Invalid array shape: expected {expected}, got {got}")]
    ShapeMismatch { expected: String, got: String },
    #[error("Coordinate conversion failed: {0}")]
    CoordError(String),
    #[error("Table topology error: {0}")]
    TopologyError(String),
}

impl From<OpsError> for PyErr {
    fn from(e: OpsError) -> Self {
        PyValueError::new_err(e.to_string())
    }
}

/// Helper: convert PyReadonlyArray to ndarray ArrayView2
fn to_view2<'a, T>(arr: &'a numpy::PyReadonlyArray<'a, T, Ix2>) -> ArrayView2<'a, T>
where
    T: numpy::Element + Clone,
{
    unsafe { arr.as_array() }
}

/// Helper: convert PyReadonlyArray to ndarray ArrayView1
fn to_view1<'a, T>(arr: &'a numpy::PyReadonlyArray<'a, T, Ix1>) -> ArrayView1<'a, T>
where
    T: numpy::Element + Clone,
{
    unsafe { arr.as_array() }
}

/// Batch coordinate conversion: pixels -> points (1/72 inch)
/// Input: (N, 4) float32 array [x1, y1, x2, y2] in pixel coordinates
/// Output: (N, 4) float32 array in points
#[pyfunction]
#[pyo3(signature = (arr, dpi))]
fn coords_px_to_pt<'py>(
    py: Python<'py>,
    arr: Bound<'py, PyArray<f32, Ix2>>,
    dpi: f32,
) -> PyResult<Bound<'py, PyArray<f32, Ix2>>> {
    let scale = 72.0 / dpi;
    let src = arr.readonly();
    let shape = src.shape();
    
    if shape.len() != 2 || shape[1] != 4 {
        return Err(OpsError::ShapeMismatch {
            expected: "(N, 4)".into(),
            got: format!("{:?}", shape),
        }.into());
    }

    let n_rows = shape[0];
    let mut out = Array2::<f32>::zeros((n_rows, 4));
    
    // Convert to ndarray view for fast indexing
    let src_view = to_view2(&src);

    // The pipeline calls this with a single bbox (shape (1, 4)). Starting
    // rayon's global pool (one thread per CPU core) for that is pure overhead
    // and adds threads that compete with OCR inference. Stay serial for small
    // inputs; parallelise only genuinely large batches.
    const PAR_THRESHOLD: usize = 1024;
    if n_rows >= PAR_THRESHOLD {
        let mut out_view = out.view_mut();
        out_view.axis_iter_mut(Axis(0))
            .zip(src_view.axis_iter(Axis(0)))
            .par_bridge()
            .for_each(|(mut d, s)| {
                d.iter_mut().zip(s.iter()).for_each(|(d_val, s_val)| *d_val = *s_val * scale);
            });
    } else {
        let mut out_view = out.view_mut();
        for (mut d, s) in out_view.axis_iter_mut(Axis(0)).zip(src_view.axis_iter(Axis(0))) {
            d.iter_mut().zip(s.iter()).for_each(|(d_val, s_val)| *d_val = *s_val * scale);
        }
    }

    Ok(out.into_pyarray(py))
}

/// Batch coordinate conversion: points -> EMU (English Metric Units, 1/914400 inch)
/// Used for precise OOXML positioning
#[pyfunction]
#[pyo3(signature = (arr))]
fn coords_pt_to_emu<'py>(
    py: Python<'py>,
    arr: Bound<'py, PyArray<f32, Ix2>>,
) -> PyResult<Bound<'py, PyArray<i64, Ix2>>> {
    const PT_TO_EMU: f32 = 12700.0;
    let src = arr.readonly();
    let shape = src.shape();
    
    if shape.len() != 2 || shape[1] != 4 {
        return Err(OpsError::ShapeMismatch {
            expected: "(N, 4)".into(),
            got: format!("{:?}", shape),
        }.into());
    }

    let n_rows = shape[0];
    let mut out = Array2::<i64>::zeros((n_rows, 4));
    
    let src_view = to_view2(&src);

    // See coords_px_to_pt: small batches stay serial to avoid waking rayon's
    // CPU-core-sized thread pool for a handful of values.
    const PAR_THRESHOLD: usize = 1024;
    if n_rows >= PAR_THRESHOLD {
        let mut out_view = out.view_mut();
        out_view.axis_iter_mut(Axis(0))
            .zip(src_view.axis_iter(Axis(0)))
            .par_bridge()
            .for_each(|(mut d, s)| {
                d.iter_mut().zip(s.iter()).for_each(|(d_val, s_val)| *d_val = (*s_val * PT_TO_EMU).round() as i64);
            });
    } else {
        let mut out_view = out.view_mut();
        for (mut d, s) in out_view.axis_iter_mut(Axis(0)).zip(src_view.axis_iter(Axis(0))) {
            d.iter_mut().zip(s.iter()).for_each(|(d_val, s_val)| *d_val = (*s_val * PT_TO_EMU).round() as i64);
        }
    }

    Ok(out.into_pyarray(py))
}

/// Comparator for table cells: row -> col -> y1 -> x1
fn compare_table_cells(v: &Array2<i32>, a: usize, b: usize) -> Ordering {
    v[[a, 2]].cmp(&v[[b, 2]])              // row_id
        .then(v[[a, 3]].cmp(&v[[b, 3]]))    // col_id
        .then(v[[a, 1]].cmp(&v[[b, 1]]))    // y1
        .then(v[[a, 0]].cmp(&v[[b, 0]]))    // x1
}

/// Sort table cells in reading order (row-major, then top-left)
/// Input: (N, 4) int32 array [x1, y1, row_id, col_id]
/// Output: 1D array of indices representing sorted order
#[pyfunction]
fn sort_table_cells(cells: Bound<'_, PyArray<i32, Ix2>>) -> PyResult<Vec<usize>> {
    let src = cells.readonly();
    let shape = src.shape();
    
    if shape.len() != 2 || shape[1] != 4 {
        return Err(OpsError::ShapeMismatch {
            expected: "(N, 4)".into(),
            got: format!("{:?}", shape),
        }.into());
    }

    let n = shape[0];
    if n == 0 {
        return Ok(Vec::new());
    }

    // Convert to owned ndarray for indexing
    let v = to_view2(&src).to_owned();
    let mut idx: Vec<usize> = (0..n).collect();
    
    // Tables on a page have a handful of cells; parallel sort would wake
    // rayon's CPU-core-sized pool for almost no work. Only large tables pay
    // for the parallel path.
    if n >= 512 {
        idx.par_sort_by(|&a, &b| compare_table_cells(&v, a, b));
    } else {
        idx.sort_by(|&a, &b| compare_table_cells(&v, a, b));
    }
    
    Ok(idx)
}

/// Topological sort of layout blocks based on reading order constraints
/// Input: (N, 4) float32 bboxes [x1, y1, x2, y2], (N,) int32 block_types
/// Output: 1D array of indices in reading order
#[pyfunction]
fn sort_layout_blocks(
    bboxes: Bound<'_, PyArray<f32, Ix2>>,
    block_types: Bound<'_, PyArray<i32, Ix1>>,
    img_width: f32,
    img_height: f32,
) -> PyResult<Vec<usize>> {
    let boxes_arr = bboxes.readonly();
    let types_arr = block_types.readonly();
    let box_shape = boxes_arr.shape();
    let type_shape = types_arr.shape();
    
    if box_shape.len() != 2 || box_shape[1] != 4 {
        return Err(OpsError::ShapeMismatch {
            expected: "(N, 4)".into(),
            got: format!("{:?}", box_shape),
        }.into());
    }
    
    let n = box_shape[0];
    if n == 0 {
        return Ok(Vec::new());
    }
    
    if type_shape.len() != 1 || type_shape[0] != n {
        return Err(OpsError::ShapeMismatch {
            expected: format!("(N,) with N={}", n),
            got: format!("{:?}", type_shape),
        }.into());
    }

    // Convert to ndarray views for fast indexing
    let boxes = to_view2(&boxes_arr);
    let types = to_view1(&types_arr);

    // Build DAG: edges represent "must come before" reading order
    let mut graph = DiGraph::<usize, ()>::new();
    let node_indices: Vec<NodeIndex> = (0..n).map(|_| graph.add_node(0)).collect();

    // Heuristics for reading order constraints
    for i in 0..n {
        let x1_i = boxes[[i, 0]];
        let y1_i = boxes[[i, 1]];
        let x2_i = boxes[[i, 2]];
        let y2_i = boxes[[i, 3]];
        let type_i = types[i];
        let cx_i = (x1_i + x2_i) * 0.5;
        let cy_i = (y1_i + y2_i) * 0.5;

        for j in 0..n {
            if i == j { continue; }

            let x1_j = boxes[[j, 0]];
            let y1_j = boxes[[j, 1]];
            let x2_j = boxes[[j, 2]];
            let y2_j = boxes[[j, 3]];
            let type_j = types[j];
            let cx_j = (x1_j + x2_j) * 0.5;
            let cy_j = (y1_j + y2_j) * 0.5;

            // Same column (overlap in x), i above j -> i before j
            let x_overlap = (x1_i.max(x1_j) < x2_i.min(x2_j)) as i32;
            let y_order = (cy_i < cy_j) as i32;

            // Left-to-right for same row (overlap in y)
            let y_overlap = (y1_i.max(y1_j) < y2_i.min(y2_j)) as i32;
            let x_order = (cx_i < cx_j) as i32;

            // Multi-column detection: if blocks are side-by-side with gap
            let gap_x = (x1_j - x2_i).abs().min((x1_i - x2_j).abs());
            let is_multi_col = gap_x > img_width * 0.05 && y_overlap == 1;

            let should_precede = match (type_i, type_j) {
                // Title/header before body
                (0, 1) | (0, 2) => true,  // title -> text/figure
                // Text before figure/table in same column
                (1, 2) | (1, 3) if x_overlap == 1 && y_order == 1 => true,
                // Figure before caption
                (2, 4) if x_overlap == 1 && y_order == 1 => true,
                // Table before caption
                (3, 4) if x_overlap == 1 && y_order == 1 => true,
                // General top-to-bottom in same column
                _ if x_overlap == 1 && y_order == 1 && !is_multi_col => true,
                // Left-to-right for multi-column
                _ if is_multi_col && x_order == 1 => true,
                _ => false,
            };

            if should_precede {
                graph.add_edge(node_indices[i], node_indices[j], ());
            }
        }
    }

    // Topological sort with stable tie-breaking (top-left first)
    let order = toposort(&graph, None)
        .map_err(|_| OpsError::TopologyError("Cycle detected in layout graph".into()))?;

    // Convert NodeIndex back to original indices
    let result: Vec<usize> = order.iter().map(|&nx| nx.index()).collect();
    Ok(result)
}

/// Non-maximum suppression for overlapping boxes
/// Input: (N, 4) float32 boxes, (N,) float32 scores, iou_threshold
/// Output: 1D array of kept indices
#[pyfunction]
fn nms_boxes(
    boxes: Bound<'_, PyArray<f32, Ix2>>,
    scores: Bound<'_, PyArray<f32, Ix1>>,
    iou_threshold: f32,
) -> PyResult<Vec<usize>> {
    let boxes_arr = boxes.readonly();
    let scores_arr = scores.readonly();
    let box_shape = boxes_arr.shape();
    let score_shape = scores_arr.shape();
    
    if box_shape.len() != 2 || box_shape[1] != 4 {
        return Err(OpsError::ShapeMismatch { expected: "(N, 4)".into(), got: format!("{:?}", box_shape) }.into());
    }
    let n = box_shape[0];
    if n == 0 { return Ok(Vec::new()); }
    if score_shape.len() != 1 || score_shape[0] != n { return Err(OpsError::ShapeMismatch { expected: format!("(N,) with N={}", n), got: format!("{:?}", score_shape) }.into()); }
    
    // Convert to ndarray views
    let b = to_view2(&boxes_arr);
    let s = to_view1(&scores_arr);
    
    // Sort by score descending
    let mut idx: Vec<usize> = (0..n).collect();
    idx.sort_by(|&a, &b| s[b].partial_cmp(&s[a]).unwrap());
    
    let mut keep = Vec::with_capacity(n);
    let mut suppressed = vec![false; n];
    
    for &i in &idx {
        if suppressed[i] { continue; }
        keep.push(i);
        
        let x1_i = b[[i, 0]];
        let y1_i = b[[i, 1]];
        let x2_i = b[[i, 2]];
        let y2_i = b[[i, 3]];
        let area_i = (x2_i - x1_i).max(0.0) * (y2_i - y1_i).max(0.0);
        
        for &j in &idx {
            if i == j || suppressed[j] { continue; }
            
            let x1_j = b[[j, 0]];
            let y1_j = b[[j, 1]];
            let x2_j = b[[j, 2]];
            let y2_j = b[[j, 3]];
            let area_j = (x2_j - x1_j).max(0.0) * (y2_j - y1_j).max(0.0);
            
            let ix1 = x1_i.max(x1_j);
            let iy1 = y1_i.max(y1_j);
            let ix2 = x2_i.min(x2_j);
            let iy2 = y2_i.min(y2_j);
            
            let inter_w = (ix2 - ix1).max(0.0);
            let inter_h = (iy2 - iy1).max(0.0);
            let inter = inter_w * inter_h;
            let union = area_i + area_j - inter;
            
            if union > 0.0 && inter / union > iou_threshold {
                suppressed[j] = true;
            }
        }
    }
    
    Ok(keep)
}

/// Merge adjacent/overlapping text lines into paragraphs
/// Input: (N, 4) float32 boxes, (N,) float32 line_heights, vertical_threshold_ratio
/// Output: 2D array [paragraph_id, line_indices...] as list of lists
#[pyfunction]
fn merge_text_lines(
    boxes: Bound<'_, PyArray<f32, Ix2>>,
    line_heights: Bound<'_, PyArray<f32, Ix1>>,
    v_threshold_ratio: f32,
) -> PyResult<Vec<Vec<usize>>> {
    let boxes_arr = boxes.readonly();
    let heights_arr = line_heights.readonly();
    let box_shape = boxes_arr.shape();
    let h_shape = heights_arr.shape();
    
    if box_shape.len() != 2 || box_shape[1] != 4 {
        return Err(OpsError::ShapeMismatch { expected: "(N, 4)".into(), got: format!("{:?}", box_shape) }.into());
    }
    let n = box_shape[0];
    if n == 0 { return Ok(Vec::new()); }
    if h_shape.len() != 1 || h_shape[0] != n {
        return Err(OpsError::ShapeMismatch { expected: format!("(N,) with N={}", n), got: format!("{:?}", h_shape) }.into());
    }

    // Convert to ndarray views
    let b = to_view2(&boxes_arr);
    let h = to_view1(&heights_arr);

    // Sort by y1 (top to bottom)
    let mut idx: Vec<usize> = (0..n).collect();
    idx.sort_by(|&a, &b_idx| b[[a, 1]].partial_cmp(&b[[b_idx, 1]]).unwrap());
    
    let mut paragraphs = Vec::new();
    let mut current_para = vec![idx[0]];
    let mut last_bottom = b[[idx[0], 3]];
    let mut avg_height = h[idx[0]];
    
    for &i in &idx[1..] {
        let y1 = b[[i, 1]];
        let gap = y1 - last_bottom;
        
        if gap <= avg_height * v_threshold_ratio {
            current_para.push(i);
            avg_height = (avg_height * (current_para.len() - 1) as f32 + h[i]) / current_para.len() as f32;
        } else {
            paragraphs.push(current_para);
            current_para = vec![i];
            avg_height = h[i];
        }
        last_bottom = b[[i, 3]];
    }
    paragraphs.push(current_para);
    
    Ok(paragraphs)
}

/// Compute optimal font size for a text box to fit in given bbox
/// Input: text_length (chars), bbox_width_pt, bbox_height_pt, avg_char_width_ratio (default 0.5)
/// Output: font_size_pt
#[pyfunction]
fn compute_fit_font_size(
    text_length: usize,
    bbox_width_pt: f32,
    bbox_height_pt: f32,
    avg_char_width_ratio: Option<f32>,
) -> f32 {
    let ratio = avg_char_width_ratio.unwrap_or(0.5);
    if text_length == 0 { return 12.0; }
    
    let char_width = |fs: f32| fs * ratio;
    let chars_per_line = |fs: f32| (bbox_width_pt / char_width(fs)).max(1.0);
    let lines_needed = |fs: f32| (text_length as f32 / chars_per_line(fs)).ceil();
    let font_size = |fs: f32| bbox_height_pt / lines_needed(fs);
    
    let mut fs = 12.0;
    for _ in 0..10 {
        let new_fs = font_size(fs);
        if (new_fs - fs).abs() < 0.1 { break; }
        fs = new_fs;
    }
    fs.clamp(6.0, 72.0)
}

#[pymodule]
fn pdf2word_ops(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(coords_px_to_pt, m)?)?;
    m.add_function(wrap_pyfunction!(coords_pt_to_emu, m)?)?;
    m.add_function(wrap_pyfunction!(sort_table_cells, m)?)?;
    m.add_function(wrap_pyfunction!(sort_layout_blocks, m)?)?;
    m.add_function(wrap_pyfunction!(nms_boxes, m)?)?;
    m.add_function(wrap_pyfunction!(merge_text_lines, m)?)?;
    m.add_function(wrap_pyfunction!(compute_fit_font_size, m)?)?;
    Ok(())
}