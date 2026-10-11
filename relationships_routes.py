"""HTTP boundary for the relationship graph.

Read-only: this endpoint describes the library, it never changes it. Query
parameters are `layers` (comma separated node types, omitted means all of them)
and `arrangement` (`clusters` or `wheel`); the graph itself is built in
relationships.py and drawn in static/relationships.js.
"""
from flask import Blueprint, jsonify, request

from relationships import parse_arrangement, parse_layers


def create_relationships_blueprint(get_relationships):
    """Create the relationship endpoints around an app-owned graph provider."""
    blueprint = Blueprint('relationships', __name__)

    @blueprint.route('/api/relationships')
    def relationship_graph():
        payload = get_relationships().payload(
            parse_layers(request.args.get('layers')),
            parse_arrangement(request.args.get('arrangement')))
        return jsonify(payload)

    return blueprint
