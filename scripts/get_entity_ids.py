"""
Get the IDs of all the entities of a certain type in the Parliament website
"""
import argparse
import json
import math
import os
import re
from collections import defaultdict
from itertools import chain
from urllib.parse import urljoin

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.wait import WebDriverWait

TIMEOUT = 20
CACHE_DIR = 'cache'

URL = 'http://www.parlamento.pt'
# Button to click to load the search for a specific legislature
SEARCH_XPATH = '//input[@value="Pesquisar"]'

# Element containing number of results for a search, to calculate how
# many pages have to be iterated (results per page also used for that).
RESULTS_XPATH = '//span[contains(@id, "lblResults")]'
RESULTS_PER_PAGE = 20

# Element in a pager at the bottom. The only number without a link is the
# current page
CURRENT_PAGE_XPATH = '//tr[@class="{}"]//tr/td/span[text()="{}"]'
PAGER_LABEL = 'ARLabel'

# Link containing the ID to be retrieved, different for each entity
ENTITY_XPATH = '//a[contains(@id, "{}")]'
# Link containing session number in attendance pages
NUMBER_XPATH = '//a[contains(@title, "{}")]'
# Common parts of the string in the link to switch pages. The variable part
# is included in the dictionary of the entity
TYPE_STRING = 'ctl00$ctl43${}$ctl00$gvResults'
# Dictionary of entity-specific data. Add entries here to allow scraping of
# other entities.
TARGET_DICT = {
    'mp': {
        'path': '/DeputadoGP/Paginas/Deputados.aspx?more=1',
        'type_string': 'g_4090e9c6_d794_4506_9ff9_3e6f8d30ec2d',
        'legislature_label': 'Legislatura',
        'id_label': 'hplNome',
    },
    'initiative': {
        'path': '/ActividadeParlamentar/Paginas/IniciativasLegislativas.aspx',
        'type_string': 'g_889e27d8_462c_47cc_afea_c4a07765d8c7',
        'legislature_label': 'ddlLeg',
        'id_label': 'hplTitulo',
        'session_label': 'ddlSL',
    },
    'attendance': {
        'path': '/DeputadoGP/Paginas/reunioesplenarias.aspx',
        'type_string': 'g_90441d47_53a9_460e_a62f_b50c50d57276',
        'legislature_label': 'Legislatura',
        'id_label': 'hplData',
        'number_label': 'n.º',
    },
}


class ParliamentIDScraper:
    """Class to scrape IDs from entities in the parliament web site"""
    def __init__(self, entity, driver, full=False, cache=True):
        # Set user options
        self.entity = entity
        self.cache = cache
        self.full = full

        # Set parameters of the entity being scraped
        self.type_params = TARGET_DICT[entity]
        self.entity_xpath = ENTITY_XPATH.format(self.type_params['id_label'])
        self.pager_string = TYPE_STRING.format(self.type_params['type_string'])
        self.url = urljoin(URL, self.type_params['path'])
        if entity == 'attendance':
            self.number_xpath = NUMBER_XPATH.format(
                self.type_params['number_label']
            )

        # Initialize result and cache dicts
        self.id_list = defaultdict(list)
        self.cache_dict = self.get_cache()

        # Setup web driver and its wait mechanism
        self.driver = driver
        self.wait = WebDriverWait(self.driver, TIMEOUT)
        self.driver.get(self.url)

    def get_cache(self):
        """
        Prepare or load cache dictionary

        This dictionary stores legislatures that were already processed and
        the ids that were retrieved, so we can output only the missing ones.
        """
        cache_dict = {
            'legislatures': [],
            'ids': set(),
        }
        if not self.cache:
            pass
        elif not os.path.isdir(CACHE_DIR):
            try:
                os.makedirs(CACHE_DIR)
            except OSError:
                print('Error creating directory {}.'.format(CACHE_DIR))
                print('Results will not be cached!')
        else:
            filename = '{}_cache.json'.format(self.entity)
            try:
                f = open(os.path.join(CACHE_DIR, filename), encoding='utf8')
            except IOError:
                print('Could not open cache file, using empty dictionary...')
            else:
                with f:
                    cache_dict = json.load(f)
                    cache_dict['ids'] = set(cache_dict['ids'])
        return cache_dict

    def get_ids(self):
        """Get all IDs from current page

        In the case of attendance, we also need to get the session number
        """
        links = self.driver.find_elements_by_xpath(self.entity_xpath)
        ids = [link.get_attribute('href').rsplit('=')[1] for link in links]
        if self.entity == 'attendance':
            number_links = self.driver.find_elements_by_xpath(self.number_xpath)
            numbers = [number_link.text for number_link in number_links]
            ids = zip(ids, numbers)
        # href is like parlamento.pt/DeputadoGP/Paginas/Biografia.aspx?BID=3
        return set(ids)

    def process_legislatures(self, legislatures):
        """Process legislatures and deal with failures.

        legislatures is a list of strings representing the legislatures in
        roman numerals (e.g. ['I', 'II', 'IX']).
        """
        for legislature in legislatures:
            if legislature in self.cache_dict['legislatures']:
                if self.full:
                    print('Skipping cached legislature {}'.format(legislature))
                    continue
            try:
                self.process_legislature(legislature)
            except WebDriverException:
                print('Failed processing {}'.format(legislature))

    def process_legislature(self, legislature):
        """Process a legislature

        legislatures is a string representing the legislature in roman
        numerals (e.g. 'XII').
        """
        print('Processing legislature {}...'.format(legislature))
        self.select_legislature(legislature)
        if 'session_label' in self.type_params:
            self.clear_session()
        search = self.wait.until(expected_conditions.element_to_be_clickable(
            (By.XPATH, SEARCH_XPATH)))
        search.click()
        self.wait.until(expected_conditions.presence_of_element_located(
                (By.XPATH, CURRENT_PAGE_XPATH.format(PAGER_LABEL, 1))))
        self.wait.until(expected_conditions.text_to_be_present_in_element(
            (By.XPATH, RESULTS_XPATH), 'Resultado'))
        results_element = self.driver.find_element_by_xpath(RESULTS_XPATH)
        results = int(re.search(r'(\d+)', results_element.text).group())
        pages = math.ceil(results / RESULTS_PER_PAGE)
        for page_number in range(1, int(pages + 1)):
            self.process_page(page_number, legislature)
        if self.cache:
            self.cache_dict['legislatures'].append(legislature)

    def process_page(self, page_number, legislature):
        """Process a page and retrieve its ids"""
        print('Processing page {}'.format(page_number))
        if page_number != 1:  # Skip page switching if it's the first page
            self.driver.execute_script("__doPostBack('{}','Page${}')".format(
                self.pager_string,
                page_number,
            ))
        self.wait.until(
            expected_conditions.presence_of_element_located(
                (By.XPATH, CURRENT_PAGE_XPATH.format(PAGER_LABEL,
                                                     page_number))))
        entity_ids = self.get_ids()
        self.id_list[legislature].extend(entity_ids - self.cache_dict['ids'])
        if self.cache:
            self.cache_dict['ids'] |= entity_ids

    def clear_session(self):
        """Clear session to iterate full legislature"""
        session_xpath = '//select[contains(@id, "{}")]/option[@value=""]'
        option_xpath = session_xpath.format(self.type_params['session_label'])
        self.wait.until(expected_conditions.element_to_be_clickable(
            (By.XPATH, option_xpath)))
        option = self.driver.find_element_by_xpath(option_xpath)
        option.click()

    def select_legislature(self, legislature):
        """Select legislature from a dropdown"""
        xpath = '//select[contains(@id, "{}")]/option[@value="{}"]'
        option_xpath = xpath.format(self.type_params['legislature_label'],
                                    legislature)
        option = self.wait.until(expected_conditions.element_to_be_clickable(
            (By.XPATH, option_xpath)))
        option.click()

    def get_legislatures(self):
        """Get all legislatures or just the last one, from a dropdown"""
        if self.full:
            xpath_string = '//select[contains(@id, "{}")]/option[@value!=""]'
        else:
            xpath_string = '//select[contains(@id, "{}")]/option[@selected]'
        xpath = xpath_string.format(self.type_params['legislature_label'])
        options = self.driver.find_elements_by_xpath(xpath)
        legislatures = [l.get_attribute('value') for l in options]
        return legislatures

    def main(self):
        """Get legislatures, process them, and write results and cache"""
        legislatures = self.get_legislatures()
        self.process_legislatures(legislatures)
        output_file = '{}_ids.txt'.format(self.entity)
        with open(output_file, 'w', encoding='utf8') as f:
            ids = chain.from_iterable(self.id_list.values())
            ids = list(set(ids))
            if self.entity == 'attendance':
                for attendance_id, number in sorted(
                        ids,
                        key=lambda x: int(x[0])):
                    f.write(','.join([attendance_id, number]))
                    f.write('\n')
            else:
                f.write('\n'.join(sorted(ids, key=int)))
            f.write('\n')
        if self.cache:
            self.cache_dict['ids'] = list(self.cache_dict['ids'])
            cache_file = '{}_cache.json'.format(self.entity)
            cache_path = os.path.join(CACHE_DIR, cache_file)
            with open(cache_path, 'w', encoding='utf8') as f:
                json.dump(self.cache_dict, f, indent=4)


def get_driver(driver_class, driver_path=None):
    """
    Get Selenium web driver

    driver_class must be a valid selenium webdriver class(e.g. PhantomJS)
    If driver_path is None, the webdriver executable must be in the $PATH
    Exceptions are re-raised to show all information about failures
    """
    try:
        driver_class = getattr(webdriver, driver_class)
    except AttributeError:
        print('Driver {} is not supported.'.format(driver_class))
        raise
    path = [driver_path] if driver_path else []
    try:
        driver = driver_class(*path)
    except WebDriverException:
        print('Could not execute {}'.format(driver_path))
        raise
    return driver


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='Get entity IDs')
    parser.add_argument(
        '--full',
        action='store_true',
        help='Run all legislatures and not just the last',
    )
    parser.add_argument(
        '--no-cache',
        action='store_false',
        dest='cache',
        help='Do not use cached results; do not cache results for further runs'
    )
    parser.add_argument(
        '--driver',
        default='PhantomJS',
        help='Web driver to use with Selenium (default is PhantomJS)'
    )
    parser.add_argument(
        '--driver-path',
        default=None,
        help='Path to web driver executable'
    )
    parser.add_argument(
        '--type',
        required=True,
        choices=TARGET_DICT.keys(),
        help='Type of data to retrieve'
    )
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    args_driver = get_driver(args.driver, args.driver_path)
    scraper = ParliamentIDScraper(
        args.type,
        args_driver,
        args.full,
        args.cache,
    )
    scraper.main()
