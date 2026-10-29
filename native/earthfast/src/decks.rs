//! Bridge decks side by side (roads._deck_beside): which deck parts come within touching distance of each other in
//! plan, the pair test that was a numpy pass per pair (2.4 s of 14 for 8 of Bratislava's road tiles, 2026-10-28).

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// Pairs (i, j), i < j, of parts whose samples q (part i's rows qoff[i]..qoff[i + 1]: its line every few m) come within
/// (widths[i] + widths[j]) / 2 + touch of part j's line (pts rows poff[j]..poff[j + 1]) at 2 or more samples. Parts
/// with fewer than 2 vertices are left out, as are pairs whose boxes are farther apart. The numpy version's sums.
#[pyfunction]
pub fn deck_beside<'py>(py: Python<'py>, q: PyReadonlyArray2<'py, f64>, qoff: PyReadonlyArray1<'py, i64>,
                        pts: PyReadonlyArray2<'py, f64>, poff: PyReadonlyArray1<'py, i64>,
                        widths: PyReadonlyArray1<'py, f64>, touch: f64) -> Bound<'py, PyArray2<i64>> {
    let (q, qoff, pts, poff, widths) = (q.as_array(), qoff.as_array(), pts.as_array(), poff.as_array(),
                                        widths.as_array());
    let n = poff.len().saturating_sub(1);
    let out = py.allow_threads(|| {
        let span = |k: usize| (poff[k] as usize, poff[k + 1] as usize);
        // each part's box
        let mut bb = vec![[0.0f64; 4]; n];
        for k in 0..n {
            let (a, b) = span(k);
            if b > a {
                let mut r = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
                for v in a..b {
                    r[0] = r[0].min(pts[[v, 0]]);
                    r[1] = r[1].min(pts[[v, 1]]);
                    r[2] = r[2].max(pts[[v, 0]]);
                    r[3] = r[3].max(pts[[v, 1]]);
                }
                bb[k] = r;
            }
        }
        let mut out: Vec<i64> = Vec::new();
        for i in 0..n {
            let (ia, ib) = span(i);
            if ib < ia + 2 {
                continue;
            }
            let (qa, qb) = (qoff[i] as usize, qoff[i + 1] as usize);
            for j in i + 1..n {
                let (ja, jb) = span(j);
                if jb < ja + 2 {
                    continue;
                }
                let reach = (widths[i] + widths[j]) / 2.0 + touch;
                let (bi, bj) = (bb[i], bb[j]);
                if bi[2] + reach < bj[0] || bi[3] + reach < bj[1] || bj[2] + reach < bi[0] || bj[3] + reach < bi[1] {
                    continue;
                }
                let mut hits = 0;
                for s in qa..qb {
                    let (qx, qy) = (q[[s, 0]], q[[s, 1]]);
                    let mut d = f64::INFINITY;
                    for v in ja..jb - 1 {
                        let (px, py_) = (pts[[v, 0]], pts[[v, 1]]);
                        let (abx, aby) = (pts[[v + 1, 0]] - px, pts[[v + 1, 1]] - py_);
                        let l2 = (abx * abx + aby * aby).max(1e-12);
                        let t = (((qx - px) * abx + (qy - py_) * aby) / l2).clamp(0.0, 1.0);
                        d = d.min((qx - (px + t * abx)).hypot(qy - (py_ + t * aby)));
                    }
                    if d < reach {
                        hits += 1;
                        if hits >= 2 {
                            break;
                        }
                    }
                }
                if hits >= 2 {
                    out.push(i as i64);
                    out.push(j as i64);
                }
            }
        }
        out
    });
    let k = out.len() / 2;
    numpy::ndarray::Array2::from_shape_vec((k, 2), out).expect("pairs").into_pyarray(py)
}
