import hashlib
import json
from pathlib import Path
import numpy as np

def bounded_crop(reference, template, shape, source_shape, placement, crop, meta, source_info):
    ref = json.loads(Path(reference).read_text(encoding='utf-8'))
    if hashlib.sha256(Path(template).read_bytes()).hexdigest() != ref['template_sha256']:
        raise ValueError('Lens reference is bound to a different template')
    if list(shape) != ref['canvas_shape'] or list(source_shape) != ref['source_shape']:
        raise ValueError('Lens reference has a different image sampling geometry')
    if list(placement) != ref['placement_xy'] or list(crop) != ref['source_active_on_canvas_xywh']:
        raise ValueError('Lens reference has a different sensor ROI')
    if source_info.get('LensModel') != ref['lens_model'] or abs(meta['focal_length']-ref['focal_length']) > 1e-5:
        raise ValueError('Lens reference lens/focal length does not match')
    if source_info.get('FocusDistance') is None or abs(source_info['FocusDistance']-ref['focus_distance']) > 1e-5:
        raise ValueError('Lens reference focus setting does not match')
    coeff = np.asarray(ref['coefficients'], dtype=np.float64)
    if coeff.shape != (3,6) or np.any(coeff[:,4:] != 0) or ref['pixel_aspect_ratio'] != 1:
        raise ValueError('Only audited radial-only square-pixel reference is supported')
    height,width = shape
    cx,cy = np.asarray(ref['centre'])*[width,height]
    radius2 = max(cx,width-cx)**2 + max(cy,height-cy)**2
    x,y,w,h = crop
    margin = ref['source_margin_pixels']
    limits = (x+margin,y+margin,x+w-1-margin,y+h-1-margin)

    def mapped_bounds(rect, full=False):
        left,top,rw,rh=rect
        if full:

            grids=((np.arange(left,left+rw)[None,:],np.arange(a,min(a+64,top+rh))[:,None])
                   for a in range(top,top+rh,64))
        else:
            xx=np.arange(left,left+rw); yy=np.arange(top,top+rh)
            grids=[(np.concatenate((xx,xx,np.full(rh,left),np.full(rh,left+rw-1))),
                    np.concatenate((np.full(rw,top),np.full(rw,top+rh-1),yy,yy)))]
        lo=np.array([np.inf,np.inf]);hi=-lo
        for xx,yy in grids:
            dx=xx-cx;dy=yy-cy;rr=np.minimum((dx*dx+dy*dy)/radius2,1)
            for k0,k1,k2,k3,_,_ in coeff:
                ratio=((k3*rr+k2)*rr+k1)*rr+k0
                sx=cx+dx*ratio;sy=cy+dy*ratio
                lo=np.minimum(lo,[sx.min(),sy.min()]);hi=np.maximum(hi,[sx.max(),sy.max()])
        return np.r_[lo,hi]

    def valid(bounds):
        return bool(bounds[0]>=limits[0] and bounds[1]>=limits[1] and
                    bounds[2]<=limits[2] and bounds[3]<=limits[3])

    import math
    units=math.gcd(w,h);uw=w//units;uh=h//units
    chosen=None
    for n in range(units,0,-1):
        rw,rh=uw*n,uh*n
        candidate=(x+(w-rw)//2,y+(h-rh)//2,rw,rh)
        if valid(mapped_bounds(candidate)):
            chosen=candidate;break
    if chosen is None:
        raise ValueError('No crop fits the captured ROI under this warp')
    verified_bounds=mapped_bounds(chosen,full=True)
    if not valid(verified_bounds):
        raise ValueError('Interior warp support audit failed')
    return chosen,dict(reference=str(Path(reference).resolve()),
                       reference_sha256=hashlib.sha256(Path(reference).read_bytes()).hexdigest(),
                       opcode_list3_sha256=ref['opcode_list3_sha256'],
                       original_crop_xywh=list(crop),safe_crop_xywh=list(chosen),
                       mapped_source_bounds_xyxy=verified_bounds.tolist(),
                       allowed_source_bounds_xyxy=list(limits),source_margin_pixels=margin,
                       checked_output_pixels=chosen[2]*chosen[3],
                       method='All retained output pixel centres, all three radial warp planes; no pixel resampling',
                       limitation='Analytical support guard; Adobe application rendering and bright-scene lens geometry require a separate visual match')
