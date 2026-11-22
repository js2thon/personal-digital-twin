from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from app.modules.ontology import rdf_quality


EX = Namespace("https://example.org/quality#")


def _graph():
    g = Graph()
    g.add((EX.Ontology, RDF.type, OWL.Ontology))
    g.add((EX.Person, RDF.type, OWL.Class))
    g.add((EX.Person, RDFS.label, Literal("人员")))
    g.add((EX.name, RDF.type, OWL.DatatypeProperty))
    g.add((EX.name, RDFS.label, Literal("姓名")))
    g.add((EX.name, RDFS.domain, EX.Person))
    g.add((EX.name, RDFS.range, XSD.string))
    return g


def test_valid_ontology_has_no_errors():
    report = rdf_quality.analyze(_graph())
    assert report["summary"]["verdict"] == "pass"
    assert report["summary"]["classes"] == 1
    assert report["summary"]["datatype_properties"] == 1


def test_detects_missing_object_property_domain_range():
    g = _graph()
    g.add((EX.knows, RDF.type, OWL.ObjectProperty))
    report = rdf_quality.analyze(g)
    codes = {x["code"] for x in report["issues"]}
    assert "OBJECT_PROPERTY_DOMAIN_MISSING" in codes
    assert "OBJECT_PROPERTY_RANGE_MISSING" in codes


def test_detects_subclass_cycle():
    g = _graph()
    g.add((EX.Employee, RDF.type, OWL.Class))
    g.add((EX.Person, RDFS.subClassOf, EX.Employee))
    g.add((EX.Employee, RDFS.subClassOf, EX.Person))
    report = rdf_quality.analyze(g)
    assert any(x["code"] == "SUBCLASS_CYCLE" for x in report["issues"])
    assert report["summary"]["verdict"] == "block"


def test_reports_unknown_class_reference():
    g = _graph()
    g.add((EX.reportsTo, RDF.type, OWL.ObjectProperty))
    g.add((EX.reportsTo, RDFS.domain, EX.Person))
    g.add((EX.reportsTo, RDFS.range, EX.UnknownRole))
    report = rdf_quality.analyze(g)
    assert any(x["code"] == "PROPERTY_CLASS_REFERENCE_UNKNOWN" for x in report["issues"])


def test_selftest():
    assert rdf_quality.selftest()["passed"] is True
