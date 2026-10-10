"""HTTP boundary for the USB export service."""
from flask import Blueprint, jsonify, request


def create_usb_export_blueprint(get_exporter):
    """Create USB endpoints around an app-owned exporter provider."""
    blueprint = Blueprint('usb_export', __name__)

    @blueprint.route('/api/export/usb/volumes')
    def usb_export_volumes():
        return jsonify({'volumes': get_exporter().available_volumes()})

    @blueprint.route('/api/export/usb/plan', methods=['POST'])
    def plan_usb_export():
        data = request.json or {}
        crate_id, crate_ids, error = _parse_crate_selection(data)
        if error:
            return jsonify({'error': error}), 400
        try:
            plan = get_exporter().plan(
                str(data.get('mode') or '').strip(),
                str(data.get('scope') or '').strip(),
                data.get('volume'), crate_id,
                str(data.get('layout') or 'library').strip(), crate_ids)
        except (ValueError, TypeError) as error:
            return jsonify({'error': str(error)}), 400
        return jsonify({
            'mode': plan['mode'], 'scope': plan['scope'], 'layout': plan['layout'],
            'crate_id': crate_id, 'crate_ids': crate_ids, 'crate_name': plan['crate_name'],
            'track_count': plan['track_count'], 'copy_count': plan['copy_count'],
            'missing_count': len(plan['missing']), 'unsupported_count': plan['unsupported'],
            'empty_crates': plan['empty_crates'], 'bytes_required': plan['total_bytes'],
            'bytes_free': plan['free_bytes'], 'portable_playlists': True,
            'player_library_created': False,
        })

    @blueprint.route('/api/export/usb', methods=['POST'])
    def start_usb_export():
        data = request.json or {}
        crate_id, crate_ids, error = _parse_crate_selection(data)
        if error:
            return jsonify({'error': error}), 400
        try:
            exporter = get_exporter()
            plan = exporter.plan(
                str(data.get('mode') or '').strip(),
                str(data.get('scope') or '').strip(),
                data.get('volume'), crate_id,
                str(data.get('layout') or 'library').strip(), crate_ids)
            job_id = exporter.start(plan)
        except (ValueError, TypeError, RuntimeError) as error:
            return jsonify({'error': str(error)}), 409 if isinstance(error, RuntimeError) else 400
        return jsonify({'ok': True, 'job_id': job_id})

    @blueprint.route('/api/export/usb/status/<job_id>')
    def usb_export_status(job_id):
        status = get_exporter().status(job_id)
        if status is None:
            return jsonify({'error': 'Unknown USB export job.'}), 404
        return jsonify(status)

    return blueprint


def _parse_crate_selection(data):
    crate_id = data.get('crate_id')
    try:
        crate_id = int(crate_id) if crate_id is not None else None
    except (TypeError, ValueError):
        return None, None, 'Invalid crate ID.'
    crate_ids = data.get('crate_ids')
    if crate_ids is not None:
        if not isinstance(crate_ids, list):
            return None, None, 'Invalid crate selection.'
        try:
            if any(isinstance(crate_id, bool) for crate_id in crate_ids):
                raise ValueError
            crate_ids = list(dict.fromkeys(int(crate_id) for crate_id in crate_ids))
        except (TypeError, ValueError):
            return None, None, 'Invalid crate selection.'
    return crate_id, crate_ids, None
