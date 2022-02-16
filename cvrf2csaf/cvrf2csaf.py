import logging
import argparse
import json
import os
import re

from lxml import etree
from lxml import objectify
from jsonschema import Draft202012Validator, ValidationError, SchemaError, draft202012_format_checker

from .common.utils import get_config_from_file, store_json, critical_exit, create_file_name

from .section_handlers.document_leaf_elements import DocumentLeafElements
from .section_handlers.document_acknowlegments import Acknowledgments
from .section_handlers.notes import Notes
from .section_handlers.document_publisher import DocumentPublisher
from .section_handlers.references import References
from .section_handlers.document_tracking import DocumentTracking
from .section_handlers.product_tree import ProductTree
from .section_handlers.vulnerability import Vulnerability
from .common.common import SectionHandler

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(module)s - %(levelname)s - %(message)s')


class DocumentHandler:
    """
    Main Handler of the conversion:
    1. Reads/Parses/Validates CVRF XML input
    2. Iterates over each first-level XML section
    3. Each first-level XML section has its class and its methods are responsible for converting the content
    4. Collecting the output of each mapper class, which consists of the CSAF2.0 JSON equivalent
    5. Combining it to the final JSON and writing the result to a file
    """

    SCHEMA_FILE = 'schemata/cvrf/1.2/cvrf.xsd'
    CATALOG_FILE = 'schemata/catalog_1_2.xml'

    # Content copied from https://github.com/secvisogram/secvisogram/blob/main/app/lib/shared/Core/csaf_2.0_strict.json
    CSAF_SCHEMA_FILE = 'schemata/csaf/2.0/csaf_json_schema_strict.json'

    def __init__(self, config):
        self.document_leaf_elements = DocumentLeafElements(config=config)
        self.document_acknowledgments = Acknowledgments()
        self.document_notes = Notes()
        self.document_publisher = DocumentPublisher(config=config)
        self.document_references = References(config=config)
        self.document_tracking = DocumentTracking(config=config)
        self.product_tree = ProductTree()
        self.vulnerability = Vulnerability(config=config)

        self.sections_handlers = {
            'Acknowledgements': self.document_acknowledgments,
            'DocumentNotes': self.document_notes,
            'DocumentPublisher': self.document_publisher,
            'DocumentReferences': self.document_references,
            'DocumentTracking': self.document_tracking,
            'ProductTree': self.product_tree,
            'Vulnerability': self.vulnerability,
        }

    def _update_CVSSv3_version_from_schema(self, root_element):
        """ Tries to update CVSS 3.x version from schema."""
        cvss_3_regex = '.*cvss-v(3\.[01]).*'

        potential_cvss3 = None

        # iterate over  namespaces
        for ns in root_element.nsmap.values():
            match = re.match(cvss_3_regex, ns)
            if match:
                cvss_version_matched = match.groups()[0]
                # no potential cvss version found yet -> store it
                if not potential_cvss3:
                    potential_cvss3 = cvss_version_matched
                # already have some version, but it's the same as currently matched -> ok, continue
                elif potential_cvss3 == cvss_version_matched:
                    continue
                # else we have two different potential cvss versions -> skip this step comletely
                else:
                    return

        if potential_cvss3:
            logging.info(f'Default CVSS v3.x version set to {potential_cvss3} based on document XML schemas.')
            self.vulnerability.default_CVSS3_version = potential_cvss3

    def _parse(self, root):
        self._update_CVSSv3_version_from_schema(root)

        # Document leaf elements are handled on the root itself
        self.document_leaf_elements.create_csaf(root)

        # For children of the root element with a deeper structure, dedicated section handlers are used
        for elem in root.iterchildren():
            # get tag name without it's namespace, don't use elem.tag here
            tag = etree.QName(elem).localname
            tag_handler = self.sections_handlers.get(tag)

            if tag_handler:
                tag_handler.create_csaf(root_element=elem)

    def _compose_final_csaf(self) -> dict:
        # Merges first level leaves into final CSAF document.
        # [mapping table](https://github.com/tschmidtb51/csaf/blob/csaf-2.0-what-is-new-table/notes/whats-new-csaf-v2.0-cn01.md#e4-mapped-elements)

        final_csaf = {'document': {}}
        final_csaf['document'] = self.document_leaf_elements.csaf
        final_csaf['document']['acknowledgments'] = self.document_acknowledgments.csaf
        final_csaf['document']['notes'] = self.document_notes.csaf
        final_csaf['document']['publisher'] = self.document_publisher.csaf
        final_csaf['document']['tracking'] = self.document_tracking.csaf
        final_csaf['document']['references'] = self.document_references.csaf
        final_csaf['product_tree'] = self.product_tree.csaf
        final_csaf['vulnerabilities'] = self.vulnerability.csaf
        return final_csaf

    @classmethod
    def _validate_and_open_file(cls, file_path):
        """Read CVRF XML from $path"""
        with open(cls.SCHEMA_FILE) as f:
            os.environ.update(XML_CATALOG_FILES=cls.CATALOG_FILE)
            schema = etree.XMLSchema(file=f)

        parser = objectify.makeparser(schema=schema)

        try:
            xml_objectified = objectify.parse(file_path, parser).getroot()
            return xml_objectified
        except etree.ParseError as e:
            critical_exit(f'Input document not valid: {e}.')

    def convert_file(self, path) -> dict:
        """Wrapper to read/parse CVRF and parse it to CSAF JSON structure"""
        root = DocumentHandler._validate_and_open_file(path)

        self._parse(root)

        return self._compose_final_csaf()

    def validate_output_against_schema(self, output_file) -> bool:
        """
        Validates the CSAF output against the CSAF JSON schema
        return: True if valid, False if invalid
        """
        with open(self.CSAF_SCHEMA_FILE) as f:
            csaf_schema_content = json.loads(f.read())

        with open(output_file) as f:
            output_file_content = json.loads(f.read())

        try:
            Draft202012Validator.check_schema(csaf_schema_content)
            v = Draft202012Validator(csaf_schema_content, format_checker=draft202012_format_checker)
            v.validate(output_file_content)
        except SchemaError as e:
            logging.error(f'CSAF schema validation error. Provided CSAF schema is invalid. Message: {e.message}')
            return False
        except ValidationError as e:
            logging.error(f'CSAF schema validation error. Path: {e.json_path}. Message: {e.message}.')
            return False
        else:
            logging.info('CSAF schema validation OK')
            return True


def main():
    # General args
    parser = argparse.ArgumentParser(description='Converts CVRF XML input into CSAF 2.0 JSON output.')
    parser.add_argument('--input-file', dest='input_file', type=str, required=True,
                        help="CVRF XML input file to parse", metavar='PATH')
    parser.add_argument('--output-dir', dest='output_dir', type=str, required=True,
                        help="CVRF JSON output dir to write to. Filename is derived from /document/tracking/id.", metavar='PATH')
    parser.add_argument('--print', dest='print', action='store_true', default=False,
                        help="Additionally prints JSON output on command line.")
    parser.add_argument('--force', action='store_true', dest='force',
                        help="If used, the converter produces output that is invalid "
                             "(use case: convert to JSON, fix the errors manual, e.g. in Secvisogram.")

    # Document Publisher args
    parser.add_argument('--publisher-name', dest='publisher_name', type=str, help="Name of the publisher.")
    parser.add_argument('--publisher-namespace', dest='publisher_namespace', type=str,
                        help="Namespace of the publisher.")

    # Document Tracking args
    parser.add_argument('--force-update-revision-history', action='store_const', const='cmd-arg-entered',
                        help="If the current version is not present in the revision history AND the difference "
                             "between the current version and the most recent revision is more than one version, "
                             "the current version is added to the revision history. Also warning is produced. By default, "
                             "the current version is added only if the difference is one version.")

    args = {k: v for k, v in vars(parser.parse_args()).items() if v is not None}

    config = get_config_from_file()

    # Update & rewrite config file values with the ones from command line arguments
    config.update(args)

    # Boolean optional argument need special treatment
    if config['force_update_revision_history'] == 'cmd-arg-entered':
        config['force_update_revision_history'] = True

    if not os.path.isfile(config.get('input_file')):
        critical_exit(f'Input file not found, check the path: {config.get("input_file")}')

    # DocumentHandler is iterating over each XML element within convert_file and return CSAF 2.0 JSON
    h = DocumentHandler(config)
    final_csaf = h.convert_file(path=config.get('input_file'))

    valid_output = True
    if not h.validate_output_against_schema(config.get('output_file')) or SectionHandler.error_occurred:
        valid_output = False

        if not config.get('force', False):
            critical_exit("Some error occurred during conversion, can't produce output."
                          " To override this, use --force.")
        else:
            logging.warning('Some errors occurred during conversion,'
                            ' but producing output as --force option is used.')

    # Output / Store results
    file_name = create_file_name(final_csaf['document'].get('tracking', {}).get('id', None), valid_output)
    file_path = str(os.path.join(config.get('output_dir'), file_name))
    store_json(js=final_csaf, fpath=file_path)
    if config.get('print', False):
        print(json.dumps(final_csaf, indent=1))


if __name__ == '__main__':
    main()
