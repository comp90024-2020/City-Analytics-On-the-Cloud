import os
import json
import logging
from time import asctime, localtime, time
from tqdm import tqdm
from utils.database import CouchDB
from utils.logger import get_logger
from cloudant.design_document import DesignDocument, Document

logger = get_logger('AreasUpdater', logging.DEBUG)

couch = CouchDB()
date_time = asctime(localtime(time()))

backup_path = os.path.join(os.path.join("views_backup", date_time))

count = 1
while os.path.exists(backup_path):
    backup_path = backup_path[:backup_path.find('-')]
    backup_path += str(count)
    count += 1
    if count > 10:
        logger.error("Cannot generate a valid  backup path")
        exit()


class View:
    def __init__(self, map_, reduce):
        self.map = map_
        self.reduce = reduce


def read_view_from_file(path):
    map_file = open(os.path.join(path, 'map.js'))
    map_func = map_file.read()
    map_file.close()
    map_func = map_func[12:]

    reduce_file = open(os.path.join(path, 'reduce.js'))
    reduce_func = reduce_file.read()
    reduce_file.close()
    reduce_func = reduce_func[15:]
    if reduce_func[0] == '_':
        reduce_func = reduce_func[:reduce_func.find(';')]
    return View(map_func, reduce_func)


def update_areas():
    areas_json = []
    if "areas" in couch.client.all_dbs():
        couch.client["areas"].delete()
    if "areas" not in couch.client.all_dbs():
        filenames = os.listdir("data/LocalGovernmentAreas-2016")
        for filename in filenames:
            abs_path = os.path.join("data/LocalGovernmentAreas-2016", filename)
            if os.path.isfile(abs_path):
                with open(abs_path) as f:
                    areas = json.loads(f.read())['features']
                    for i in range(len(areas)):
                        areas[i]['properties']['states'] = filename[:filename.find('.')]
                    f.close()
                    areas_json += areas

        couch.client.create_database("areas", partitioned=False)
        no_where = {"_id": "australia",
                    "lga2016_area_code": "australia",
                    "lga2016_area_name": "In Australia But No Specific Location"}

        out_of_vitoria = {"_id": "out_of_australia",
                          "lga2016_area_code": "out_of_australia",
                          "lga2016_area_name": "Out of Australia"}

        couch.client["areas"].create_document(no_where)
        couch.client["areas"].create_document(out_of_vitoria)
        for area in tqdm(areas_json, unit=' areas'):
            area["_id"] = area['properties']['feature_code']
            area["lga2016_area_code"] = area['properties']['feature_code']
            area["lga2016_area_name"] = area['properties']['feature_name']
            couch.client["areas"].create_document(area)
    logger.info("Wrote {} areas in to couchdb".format(len(areas_json)))


def check_db(combined_db_name):
    os.mkdir(os.path.join("views_backup", date_time, combined_db_name))
    (db_name, partitioned) = combined_db_name.split('.')
    partitioned = True if partitioned == "partitioned" else False
    if db_name not in couch.client.all_dbs():
        couch.client.create_database(db_name, partitioned=partitioned)
        logger.info("Created database: {}".format(db_name))

    for ddoc_name in os.listdir(os.path.join("couch", combined_db_name)):
        if os.path.isdir(os.path.join("couch", combined_db_name, ddoc_name)):
            check_ddoc_in_db(combined_db_name, ddoc_name)


def check_ddoc_in_db(combined_db_name, combined_ddoc_name):
    os.mkdir(os.path.join("views_backup", date_time, combined_db_name, combined_ddoc_name))
    db_name = combined_db_name.split('.')[0]
    (ddoc_name, partitioned) = combined_ddoc_name.split('.')
    partitioned = True if partitioned == "partitioned" else False
    design_doc = Document(couch.client[db_name], '_design/' + ddoc_name)
    if not design_doc.exists():
        design_doc = DesignDocument(couch.client[db_name], '_design/' + ddoc_name, partitioned=partitioned)
        design_doc.save()
        logger.info("Created design document: {}".format(ddoc_name))

    for view_name in os.listdir(os.path.join("couch", combined_db_name, combined_ddoc_name)):
        if os.path.isdir(os.path.join("couch", combined_db_name, combined_ddoc_name, view_name)):
            check_a_single_view(combined_db_name, combined_ddoc_name, view_name)


def update_view(db_name, ddoc_name, view_name, local_view, partitioned):
    partitioned = True if partitioned == "partitioned" else False
    design_doc = couch.client[db_name]['_design/' + ddoc_name]
    design_doc.add_view(view_name, local_view.map, local_view.reduce, partitioned=partitioned)
    design_doc.save()
    logger.info("Updated view: {}/_design/{}/_view/{}".format(db_name, ddoc_name, view_name))


def save_js(path, content):
    f = open(path, "w+")
    f.write(content)
    f.close()


def check_a_single_view(combined_db_name, combined_ddoc_name, view_name):
    os.mkdir(os.path.join("views_backup", date_time, combined_db_name, combined_ddoc_name, view_name))

    db_name = combined_db_name.split('.')[0]
    (ddoc_name, partitioned) = combined_ddoc_name.split('.')
    partitioned = True if partitioned == "partitioned" else False
    local_view = read_view_from_file(os.path.join("couch", combined_db_name, combined_ddoc_name, view_name))
    design_doc = couch.client[db_name]['_design/' + ddoc_name]

    # backup
    map_func = "const map = " + design_doc.views[view_name]['map']
    if design_doc.views[view_name]['reduce'][0] == '_':
        reduce_func = "const reduce = " + design_doc.views[view_name]['reduce'] + ';\n'
    else:
        reduce_func = "const reduce = " + design_doc.views[view_name]['reduce']
    save_js(os.path.join("views_backup",
                         date_time, combined_db_name, combined_ddoc_name, view_name, 'map.js'), map_func)
    save_js(os.path.join("views_backup",
                         date_time, combined_db_name, combined_ddoc_name, view_name, 'reduce.js'), reduce_func)
    if view_name in design_doc.views:
        if design_doc.views[view_name]['map'] != local_view.map \
                or design_doc.views[view_name]['reduce'] != local_view.reduce:
            design_doc.delete_view(view_name)
            design_doc.save()
            update_view(db_name, ddoc_name, view_name, local_view, partitioned)
    else:
        update_view(db_name, ddoc_name, view_name, local_view, partitioned)


def check_all_dbs():
    for db_name in os.listdir("couch"):
        if os.path.isdir(os.path.join("couch", db_name)):
            check_db(db_name)
    logger.info("Views full backup is under {}".format(backup_path))


if __name__ == '__main__':
    try:
        os.mkdir('views_backup')
        os.mkdir(backup_path)
        update_areas()
        check_all_dbs()
    except Exception as e:
        logger.error(e)
