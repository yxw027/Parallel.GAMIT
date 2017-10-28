"""
Project: Parallel.Archive
Date: 3/19/17 11:41 AM
Author: Demian D. Gomez

Main script that scans the repository for new rinex files.
It PPPs the rinex files and searches the database for stations (within 100 m) with the same station 4 letter code.
If the station exists in the db, it moves the file to the archive and adds the new file to the "rinex" table.
if the station doesn't exist, then it incorporates the station with a special NetworkCode (???) and leaves the file in the repo until you assign the correct NetworkCode and add the station information.

It is invoked jusy by calling python pyArchiveService.py
Requires the config file gnss_data.cfg (in the running folder)

"""

import pyRinex
import dbConnection
import pyStationInfo
import pyArchiveStruct
import pyPPP
import pyBrdc
import sys
import os
import pyOptions
import Utils
import pyOTL
import shutil
import datetime
import time
import uuid
import pySp3
from tqdm import tqdm
import traceback
import platform
import pyJobServer
import pyEvents

# class to handle the output of the parallel processing
class callback_class():
    def __init__(self, pbar):
        self.errors = None
        self.stns = None
        self.pbar = pbar

    def callbackfunc(self, args):
        msg = args[0]
        new_stn = args[1]
        self.errors = msg
        self.stns = new_stn
        self.pbar.update(1)


def check_rinex_timespan_int(rinex, stn):

    # how many seconds difference between the rinex file and the record in the db
    stime_diff = abs((stn['ObservationSTime'] - rinex.datetime_firstObs).total_seconds())
    etime_diff = abs((stn['ObservationETime'] - rinex.datetime_lastObs).total_seconds())

    # at least four minutes different on each side
    if stime_diff <= 240 and etime_diff <= 240 and stn['Interval'] == rinex.interval:
        return False
    else:
        return True

def write_error(folder, filename, msg):

    # do append just in case...
    count = 0
    while True:
        try:
            file = open(os.path.join(folder,filename),'a')
            file.write(msg)
            file.close()
            break
        except IOError as e:
            if count < 3:
                count += 1
            else:
                raise IOError(str(e) + ' after 3 retries')
            continue
        except:
            raise

    return


def error_handle(cnn, event, crinex, folder, filename, no_db_log=False):

    # rollback any active transactions
    if cnn.active_transaction:
        cnn.rollback_transac()

    # move to the folder indicated
    try:
        if not os.path.isdir(folder):
            os.makedirs(folder)
    except OSError:
        # racing condition of two processes trying to create the same folder
        pass
    except:
        raise

    message = event['Description']

    try:
        index = 1
        # if the parent process could not read the name of the station, year and doy, then it created a uuid folder
        # in that case, it will not try to parse the filename since it will be the only file in the folder
        while os.path.isfile(os.path.join(folder, filename)):
            filename_parts = filename.split('.')
            filename = filename_parts[0][0:-1] + str(index) + '.' + filename_parts[1] + '.' + filename_parts[2]
            index += 1

        shutil.move(crinex, os.path.join(folder, filename))
    except Exception as e:
        message = 'could not move file into this folder!' + str(e) + '\n. Original error: ' + event['Description']

    error_file = filename.replace('d.Z','.log')
    write_error(folder, error_file, message)

    if not no_db_log:
        cnn.insert_event(event)

    return


def insert_data(Config, cnn, StationCode, rs_stn, rinexinfo, year, doy, retry_folder):

    # does the name of the file agree with the StationCode found in the database?
    # also, check the doy and year
    filename = rinexinfo.crinex
    if StationCode != rs_stn['StationCode'] or int(rinexinfo.date.year) != int(year) or int(rinexinfo.date.doy) != int(doy):
        # this if still remains here but we do not allow this condition any more to happen. See process_crinex_file -> if Result...
        # NO! rename the file before moving to the archive
        filename = rs_stn['StationCode'] + rinexinfo.date.ddd() + '0.' + rinexinfo.date.yyyy()[2:4] + 'd.Z'

    #try:
    # must rename filename to assign the correct network to the rinex record
    rinexinfo.rename_crinex_rinex(filename, rs_stn['NetworkCode'], rs_stn['StationCode'])

    # get the path to access the archive
    Archive = pyArchiveStruct.RinexStruct(cnn)

    # is this day already in the database?
    rsdoy = cnn.query('SELECT * FROM rinex WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationYear" = %i AND "ObservationDOY" = %i' % (rs_stn['NetworkCode'], rs_stn['StationCode'], int(rinexinfo.date.year), int(rinexinfo.date.doy)))

    rnx = rsdoy.dictresult()

    if rsdoy.ntuples() == 0:
        # this is a new day that wasn't previously in the db
        # not need to check date of multiday: done in parent function

        # possible racing condition: if the same file is in two different folders, there might be a racing condition of two processes
        # trying to make the same insert at the same time.
        # this error is handled by the parent function.

        cnn.begin_transac()
        cnn.insert('rinex', rinexinfo.record)

        # move the crinex to the archive
        # first check that all the structure exists. This might be the first file of a new station
        path2archive = os.path.join(Config.options['path'], Archive.build_rinex_path(rs_stn['NetworkCode'], rs_stn['StationCode'], rinexinfo.date.year, rinexinfo.date.doy, False))

        # again, the "archive" for this rinexinfo object is the repository
        rinexinfo.move_origin_file(path2archive)

        event = pyEvents.Event(Description='New data was found and added to the archive',
                               NetworkCode=rs_stn['NetworkCode'],
                               StationCode=rs_stn['StationCode'],
                               Year=int(rinexinfo.date.year),
                               DOY=int(rinexinfo.date.doy))

        cnn.insert_event(event)

        cnn.commit_transac()

        # this file is ready to be processed by pyScanArchive -ppp
    else:

        # before inserting a rinex_extra, verify the time span
        if not check_rinex_timespan_int(rinexinfo, rnx[0]):
            # this is the same file in the db (maybe with different sampling interval)
            # or has less observations than the present file
            # delete it from data_in and don't insert it in the database
            os.remove(rinexinfo.origin_file)
            return

        # there is a file in the db already. Add it to the rinex_extra table for later time span check (first check passed: new > current)
        # the time span check cannot be done here because we could be checking in parallel and there isn't a unique answer when more than 2 files
        cnn.begin_transac()

        path2archive = os.path.join(Config.options['path'],
                                    Archive.build_rinex_path(rs_stn['NetworkCode'], rs_stn['StationCode'],
                                                             rinexinfo.date.year, rinexinfo.date.doy, False))

        Archive.check_directory_struct(Config.options['path'], rs_stn['NetworkCode'], rs_stn['StationCode'], rinexinfo.date)

        # renaming of the file is taken care by move_origin_file
        rinexinfo.move_origin_file(path2archive)

        cnn.insert('rinex_extra', rinexinfo.record)

        event = pyEvents.Event(Description='New data was found and added to rinex_extra',
                               NetworkCode=rs_stn['NetworkCode'],
                               StationCode=rs_stn['StationCode'],
                               Year=int(rinexinfo.date.year),
                               DOY=int(rinexinfo.date.doy))
        cnn.insert_event(event)

        cnn.commit_transac()
        # these cases can be solved by running pyScanArhive -rinex (resolve conflicts)
    #except:
    #    error_handle(cnn, 'An unexpected error ocurred while inserting a record for ' + rinexinfo.crinex_path + ' : (file moved to retry folder)\n' + traceback.format_exc(), rinexinfo.crinex_path, retry_folder, filename)


def verify_rinex_multiday(cnn, rinexinfo, Config):
    # function to verify if rinex is multiday
    # returns true if parent process can continue with insert
    # returns false if file had to be moved to the retry

    # check if rinex is a multiday file (rinex with more than one day of observations)
    if rinexinfo.multiday:

        # move all the files to the repository, delete the crinex from the archive, log the event
        rnxlist = []
        for rnx in rinexinfo.multiday_rnx_list:
            rnxlist.append(rnx.rinex)
            # some other file, move it to the repository
            retry_folder = os.path.join(Config.repository_data_in_retry, 'multidays_found/' + rnx.date.yyyy() + '/' + rnx.date.ddd())
            rnx.compress_local_copyto(retry_folder)

        # if the file corresponding to this session is found, assign its object to rinexinfo
        event = pyEvents.Event(Description='%s was a multi-day rinex file. The following rinex files where generated and moved to the repository/data_in_retry: %s. The file %s did not enter the database at this time.' % (rinexinfo.origin_file, ','.join(rnxlist), rinexinfo.crinex),
                               NetworkCode=rinexinfo.NetworkCode,
                               StationCode=rinexinfo.StationCode,
                               Year=int(rinexinfo.date.year),
                               DOY=int(rinexinfo.date.doy))

        cnn.insert_event(event)

        # remove crinex from the repository (origin_file points to the repository, not to the archive in this case)
        os.remove(rinexinfo.origin_file)

        return False

    return True


def process_crinex_file(crinex, filename, data_rejected, data_retry):

    # create a uuid temporary folder in case we cannot read the year and doy from the file (and gets rejected)
    reject_folder = os.path.join(data_rejected, str(uuid.uuid4()))

    try:
        cnn = dbConnection.Cnn("gnss_data.cfg")
        Config = pyOptions.ReadOptions("gnss_data.cfg")
        # apply local configuration (path to repo) in the executing node
        crinex = os.path.join(Config.repository_data_in, crinex)
    except:
        return (traceback.format_exc() + ' open de database when processing file ' + crinex, None)

    # assume a default networkcode
    NetworkCode = 'rnx'
    # get the station code year and doy from the filename
    try:
        StationCode = crinex.split('/')[-1][0:4].lower()
        year = int(Utils.get_norm_year_str(int(crinex.split('/')[-1][9:11])))
        doy = int(crinex.split('/')[-1][4:7])
    except:
        event = pyEvents.Event(
            Description=traceback.format_exc() + ' could not read the station code, year or doy for file ' + crinex,
            EventType='error')
        error_handle(cnn, event,crinex,reject_folder,filename,True)
        return (event['Description'], None)

    # we can now make better reject and retry folders
    reject_folder = os.path.join(data_rejected, '%reason%/' + Utils.get_norm_year_str(year) + '/' + Utils.get_norm_doy_str(doy))
    retry_folder = os.path.join(data_retry, '%reason%/' + Utils.get_norm_year_str(year) + '/' + Utils.get_norm_doy_str(doy))

    try:
        # main try except block
        rinexinfo = pyRinex.ReadRinex(NetworkCode, StationCode, crinex) # type: pyRinex.ReadRinex

        # STOP! see if rinexinfo is a multiday rinex file
        if not verify_rinex_multiday(cnn, rinexinfo, Config):
            # was a multiday rinex. verify_rinex_date_multiday took care of it
            return (None, None)

        # DDG: we don't use otl coefficients because we need an approximated coordinate
        # we therefore just calculate the first coordinate without otl
        # NOTICE that we have to trust the information coming in the RINEX header (receiver type, antenna type, etc)
        # we don't have station info data! Still, good enough
        # the final PPP coordinate will be calculated by pyScanArchive on a different process

        # make sure that the file has the appropriate coordinates in the header for PPP.
        # put the correct APR coordinates in the header.
        # ppp didn't work, try using sh_rx2apr
        brdc = pyBrdc.GetBrdcOrbits(Config.brdc_path, rinexinfo.date, rinexinfo.rootdir)

        # initialize a station information object necessary to normalize the header
        stninfo = pyStationInfo.StationInfo(None, allow_empty=True)

        stninfo.AntennaCode = rinexinfo.antType
        stninfo.ReceiverCode = rinexinfo.recType
        stninfo.AntennaEast = 0
        stninfo.AntennaNorth = 0
        stninfo.AntennaHeight = rinexinfo.antOffset
        stninfo.RadomeCode = rinexinfo.antDome
        stninfo.AntennaSerial = rinexinfo.antNo
        stninfo.ReceiverSerial = rinexinfo.recNo

        # inflate the chi**2 limit to make sure it will pass (even if we get a crappy coordinate)
        rinexinfo.auto_coord(brdc, chi_limit=20)
        # normalize header to add the APR coordinate
        rinexinfo.normalize_header(stninfo)

        ppp = pyPPP.RunPPP(rinexinfo, '', Config.options, Config.sp3types, Config.sp3altrn, rinexinfo.antOffset, False, False) # type: pyPPP.RunPPP

        try:
            ppp.exec_ppp()

        except pyPPP.pyRunPPPException as e:

            # run again without inflating chi**2
            auto_coords_xyz, auto_coords_lla, auto_error = rinexinfo.auto_coord(brdc)

            if auto_coords_lla:
                # DDG: this is correct - auto_coord returns a numpy array (calculated in ecef2lla), so ppp.lat = auto_coords_lla is consistent.
                ppp.lat = auto_coords_lla[0]
                ppp.lon = auto_coords_lla[1]
                ppp.h   = auto_coords_lla[2]
                ppp.x   = auto_coords_xyz[0]
                ppp.y   = auto_coords_xyz[1]
                ppp.z   = auto_coords_xyz[2]
            else:
                if auto_error is None:
                    auto_error = 'no report!'

                raise pyPPP.pyRunPPPException('Both PPP and sh_rx2apr failed to obtain a coordinate for ' + crinex + '.\n'
                                              'The file has been moved into the rejection folder. '
                                              'Summary PPP file and error (if exists) follows:\n' + ppp.summary + '\n\n'
                                               'ERROR section:\n' + str(e) + '\nauto_coord error follows:\n' + auto_error)

        # check for unreasonable heights
        if ppp.h[0] > 9000 or ppp.h[0] < -400:

            reject_folder = reject_folder.replace('%reason%','rinex_issues')

            # elevation cannot be higher or lower than the tallest and lowest point on the Earth
            event = pyEvents.Event(
                Description=crinex + ' : unreasonable geodetic height (%.3f). RINEX file will not enter the archive.' % (ppp.h[0]),
                EventType='error',
                StationCode=StationCode,
                NetworkCode='???',
                Year=year,
                DOY=doy)

            error_handle(cnn, event, crinex, reject_folder, filename)

            return (None, None)

        Result, match, closest_stn = ppp.verify_spatial_coherence(cnn, StationCode)

        if Result:
            if match['StationCode'] == StationCode:
                # no further verification need because we don't know anything about the network code
                # even if the station code is wrong, if result is True we insert (there is only 1 match)
                insert_data(Config, cnn, StationCode, match, rinexinfo, year, doy, retry_folder)
            else:

                retry_folder = retry_folder.replace('%reason%','coord_conflicts')

                error = \
                """%s matches the coordinate of %s.%s (distance = %8.3f m) but the filename indicates it is %s.
Please verify that this file belongs to %s.%s, rename it and try again. The file was moved to the retry folder. Rename script and pSQL sentence follows:
BASH# mv %s %s
PSQL# INSERT INTO stations ("NetworkCode", "StationCode", "auto_x", "auto_y", "auto_z", "lat", "lon", "height") VALUES ('???','%s', %12.3f, %12.3f, %12.3f, %10.6f, %10.6f, %8.3f)
                """ % (crinex.replace(Config.repository_data_in,''), match['NetworkCode'], match['StationCode'], float(match['distance']), StationCode, match['NetworkCode'],
                       match['StationCode'], os.path.join(retry_folder, filename),
                       os.path.join(retry_folder,filename.replace(StationCode, match['StationCode'])),
                       StationCode, ppp.x, ppp.y, ppp.z, ppp.lat[0], ppp.lon[0], ppp.h[0])

                event = pyEvents.Event(
                    Description=error,
                    EventType='error',
                    StationCode=StationCode,
                    NetworkCode='???',
                    Year=year,
                    DOY=doy)

                error_handle(cnn, event, crinex, retry_folder, filename)

        else:
            # a number of things could have happened:
            # 1) wrong station code and more than one solution (that do not match the station code, of course)
            #    see rms.lhcl 2007 113 -> matches rms.igm0: 34.293 m, rms.igm1: 40.604 m, rms.byns: 4.819 m
            # 2) no entry in the database for this solution -> add a lock and populate the exit args

            if len(match) > 0:

                retry_folder = retry_folder.replace('%reason%','coord_conflicts')

                # no match, but we have some candidates
                matches = ', '.join(['%s.%s: %.3f m' % (m['NetworkCode'], m['StationCode'], m['distance']) for m in match])

                error = """Solution for RINEX in repository (%s %s) did not match a station code or a unique station location within 5 km. Possible cantidate(s): %s. This file has been moved to data_in_retry. pSQL sentence follows:
PSQL# INSERT INTO stations ("NetworkCode", "StationCode", "auto_x", "auto_y", "auto_z", "lat", "lon", "height") VALUES ('???','%s', %12.3f, %12.3f, %12.3f, %10.6f, %10.6f, %8.3f)
                """ % (crinex.replace(Config.repository_data_in,''), rinexinfo.date.yyyyddd(), matches, StationCode, ppp.x, ppp.y, ppp.z, ppp.lat[0], ppp.lon[0], ppp.h[0])

                event = pyEvents.Event(
                    Description=error,
                    EventType='error',
                    StationCode=StationCode,
                    NetworkCode='???',
                    Year=year,
                    DOY=doy)

                error_handle(cnn, event, crinex, retry_folder, filename)

                return (None, None)

            else:
                # only found a station removing the distance limit (could be thousands of km away!)

                # The user will have to add the metadata to the database before the file can be added, but in principle
                # no problem was detected by the process. This file will stay in this folder so that it gets analyzed again
                # but a "lock" will be added to the file that will have to be removed before the service analyzes again.
                # if the user inserted the station by then, it will get moved to the appropriate place.
                # we return all the relevant metadata to ease the insert of the station in the database

                otl = pyOTL.OceanLoading(StationCode, Config.options['grdtab'], Config.options['otlgrid'])
                # use the ppp coordinates to calculate the otl
                coeff = otl.calculate_otl_coeff(x=ppp.x, y=ppp.y, z=ppp.z)

                # add the file to the locks table so that it doesn't get processed over and over
                # this will be removed by user so that the file gets reprocessed once all the metadata is ready
                cnn.insert('locks',filename=crinex)

                # return a string with the relevant information to insert into the database (NetworkCode = default (rnx))
                return (None, [StationCode, (ppp.x, ppp.y, ppp.z), coeff, (ppp.lat[0], ppp.lon[0], ppp.h[0]), crinex])

    except pyRinex.pyRinexException as e:

        reject_folder = reject_folder.replace('%reason%','rinex_issues')

        # add more verbose output
        e.event['Description'] = e.event['Description'] + '\n' + crinex.replace(Config.repository_data_in,'') + ': (file moved to ' + reject_folder + ')'
        e.event['StationCode'] = StationCode
        e.event['NetworkCode'] = '???'
        e.event['Year'] = year
        e.event['DOY'] = doy
        # error, move the file to rejected folder
        error_handle(cnn, e.event, crinex, reject_folder, filename)

        return (None, None)

    except pyPPP.pyRunPPPException as e:

        # msg = 'Error in PPP while processing: ' + crinex + ' : \n' + str(e) + '\nThe file has been moved into the rejection folder. Summary PPP file (if exists) follows:\n' + ppp.summary
        reject_folder = reject_folder.replace('%reason%','no_ppp_solution')

        e.event['StationCode'] = StationCode
        e.event['NetworkCode'] = '???'
        e.event['Year'] = year
        e.event['DOY'] = doy

        error_handle(cnn, e.event, crinex, reject_folder, filename)

        return (None, None)

    except pyStationInfo.pyStationInfoException as e:

        retry_folder = retry_folder.replace('%reason%','station_info_exception')

        e.event['Description'] = e.event['Description'] + '. The file will stay in the repository and will be processed during the next cycle of pyArchiveService.'
        e.event['StationCode'] = StationCode
        e.event['NetworkCode'] = '???'
        e.event['Year'] = year
        e.event['DOY'] = doy

        error_handle(cnn, e.event, crinex, retry_folder, filename)

        return (None, None)

    except pyOTL.pyOTLException as e:

        retry_folder = retry_folder.replace('%reason%','otl_exception')

        e.event['Description'] = e.event['Description'] + ' while calculating OTL for ' + crinex.replace(Config.repository_data_in,'') + '. The file has been moved into the retry folder.'
        e.event['StationCode'] = StationCode
        e.event['NetworkCode'] = '???'
        e.event['Year'] = year
        e.event['DOY'] = doy

        error_handle(cnn, e.event, crinex, retry_folder, filename)

        return (None, None)

    except pyBrdc.pyBrdcException as e:

        # if PPP fails and ArchiveService tries to run sh_rnx2apr and it doesn't find the orbits, send to retry
        retry_folder = retry_folder.replace('%reason%', 'sp3_exception')

        e.event['Description'] = e.event['Description'] + ' while obtaining broadcast orbit for ' + crinex.replace(Config.repository_data_in,'') + '. Most likely PPP failed and sh_rnx2apr was run to try to the a coordinate (but failed due to missing broadcast orbit file). The file has been moved into the retry folder.'
        e.event['StationCode'] = StationCode
        e.event['NetworkCode'] = '???'
        e.event['Year'] = year
        e.event['DOY'] = doy

        error_handle(cnn, e.event, crinex, retry_folder, filename)

        return (None, None)

    except pySp3.pySp3Exception as e:

        retry_folder = retry_folder.replace('%reason%', 'sp3_exception')

        e.event['Description'] = e.event['Description'] + ' while obtaining orbit for ' + crinex.replace(Config.repository_data_in,'') + '. The file has been moved into the retry folder. Most likely bad RINEX header/data. RINEX header follows:\n%s' % (''.join(rinexinfo.get_header()))
        e.event['StationCode'] = StationCode
        e.event['NetworkCode'] = '???'
        e.event['Year'] = year
        e.event['DOY'] = doy

        # DDG: changed from rejected to retry because the logic was rejecting files when no orbit file was found
        # it should move the files to the retry folder, not to the rejected
        # another case is when the date is crewed up and there are negative or non-reasonable values for gps week
        # should split the pySp3Exception into "file not found" and "unreasonable gps week

        error_handle(cnn, e.event, crinex, retry_folder, filename)

        return (None, None)

    except dbConnection.dbErrInsert as e:

        reject_folder = reject_folder.replace('%reason%', 'duplicate_insert')

        # insert duplicate values: two parallel processes tried to insert different filenames (or the same) of the same station
        # to the db: move it to the rejected folder. The user might want to retry later. Log it in events
        # this case should be very rare
        event = pyEvents.Event(Description='Duplicate rinex insertion attempted while processing ' + crinex.replace(Config.repository_data_in,'') + ' : (file moved to rejected folder)',
                               EventType='error',
                               StationCode=StationCode,
                               NetworkCode='???',
                               Year=year,
                               DOY=doy)

        error_handle(cnn, event, crinex, reject_folder, filename)

        return (None, None)

    except Exception:

        retry_folder = retry_folder.replace('%reason%', 'general_exception')

        event = pyEvents.Event(Description=traceback.format_exc() + ' processing: ' + crinex.replace(Config.repository_data_in,'') + ' in node ' + platform.node() + ' (file moved to retry folder)', EventType='error')

        error_handle(cnn, event, crinex, retry_folder, filename, no_db_log=True)

        return (event['Description'], None)

    return (None, None)


def insert_station_w_lock(cnn, StationCode, filename, lat, lon, h, x, y, z, otl):

    rs = cnn.query("""
                    SELECT * FROM
                        (SELECT *, 2*asin(sqrt(sin((radians(%.8f)-radians(lat))/2)^2 + cos(radians(lat)) * cos(radians(%.8f)) * sin((radians(%.8f)-radians(lon))/2)^2))*6371000 AS distance
                            FROM stations WHERE "NetworkCode" like \'?%%\' AND "StationCode" = \'%s\') as DD
                        WHERE distance <= 100
                    """ % (lat, lat, lon, StationCode))

    if not rs.ntuples() == 0:
        NetworkCode = rs.dictresult()[0]['NetworkCode']
        # if it's a record that was found, update the locks with the station code
        cnn.update('locks', {'filename': filename}, NetworkCode=NetworkCode, StationCode=StationCode)
    else:
        # insert this new record in the stations table using a default network name (???)
        # this network name is a flag that tells the ArchiveService that no data should be added to this station
        # until a proper NetworkCode is assigned.

        # check if network code exists
        NetworkCode = '???'
        index = 0
        while cnn.query('SELECT * FROM stations WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\'' % (NetworkCode, StationCode)).ntuples() != 0:
            NetworkCode = hex(index).replace('0x','').rjust(3, '?')
            index += 1
            if index > 255:
                # FATAL ERROR! the networkCode exceed FF
                raise Exception(
                    'While looking for a temporary network code, ?ff was reached! Cannot continue executing pyArchiveService. Please free some temporary network codes.')

        rs = cnn.query('SELECT * FROM networks WHERE "NetworkCode" = \'%s\'' % (NetworkCode))

        cnn.begin_transac()
        if rs.ntuples() == 0:
            # create network code
            cnn.insert('networks', NetworkCode=NetworkCode, NetworkName='Temporary network for new stations')

        # insert record in stations with temporary NetworkCode
        try:
            cnn.insert('stations', NetworkCode=NetworkCode,
                       StationCode=StationCode,
                       auto_x=x,
                       auto_y=y,
                       auto_z=z,
                       Harpos_coeff_otl=otl,
                       lat=round(lat, 8),
                       lon=round(lon, 8),
                       height=round(h, 3))
        except dbConnection.dbErrInsert:
            # another process did the insert before, ignore the error
            pass
        except Exception:
            raise

        # update the lock information for this station
        cnn.update('locks', {'filename': filename}, NetworkCode=NetworkCode, StationCode=StationCode)
        cnn.commit_transac()


def output_handle(cnn, callback):

    out_messages = [outmsg.errors for outmsg in callback]
    new_stations = [outmsg.stns for outmsg in callback]

    if len([out_msg for out_msg in out_messages if out_msg]) > 0:
        tqdm.write(
            ' >> There were unhandled errors during this batch. Please check errors_pyArchiveService.log for details')
    if len([out_msg for out_msg in new_stations if out_msg]) > 0:
        tqdm.write(
            ' >> New stations were found in the repository. Please assign a network to them, remove the locks from the files and run again pyArchiveService')

    # function to print any error that are encountered during parallel execution
    for msg in out_messages:
        if msg:
            f = open('errors_pyArchiveService.log','a')
            f.write('ON ' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' an unhandled error occurred:\n')
            f.write(msg + '\n')
            f.write('END OF ERROR =================== \n\n')
            f.close()

    for nstn in new_stations:
        if nstn:
            # check the distance w.r.t the current new stations

            StationCode = nstn[0]
            x   = nstn[1][0]
            y   = nstn[1][1]
            z   = nstn[1][2]
            otl = nstn[2]
            lat = nstn[3][0]
            lon = nstn[3][1]
            h   = nstn[3][2]

            filename = nstn[4]


            # logic behind this sql sentence:
            # we are searching for a station within 100 meters that has been recently added, so NetworkCode = ???
            # we also force the StationName to be equal to that of the incoming RINEX to avoid having problems with
            # stations that are within 100 m (misidentifying IGM1 for IGM0, for example).
            # This logic assumes that stations within 100 m do not have the same name!
            insert_station_w_lock(cnn,StationCode,filename,lat,lon,h,x,y,z,otl)

    return []


def remove_empty_folders(folder):

    for dirpath, _, files in os.walk(folder, topdown=False):  # Listing the files
        for file in files:
            if file.endswith('DS_Store'):
                # delete the stupid mac files
                try:
                    os.remove(os.path.join(dirpath, file))
                except:
                    sys.exc_clear()
        if dirpath == folder:
            break
        try:
            os.rmdir(dirpath)
        except OSError:
            sys.exc_clear()

    return


def main():

    # bind to the repository directory

    Config = pyOptions.ReadOptions('gnss_data.cfg')

    if not os.path.isdir(Config.repository):
        print "the provided repository path in gnss_data.cfg is not a folder"
        exit()

    # initialize the PP job server
    JobServer = pyJobServer.JobServer(Config)

    cnn = dbConnection.Cnn('gnss_data.cfg')
    # create the execution log
    cnn.insert('executions', script='pyArchiveService.py')

    # set the data_xx directories
    data_in = os.path.join(Config.repository,'data_in')
    data_in_retry = os.path.join(Config.repository, 'data_in_retry')
    data_reject = os.path.join(Config.repository, 'data_rejected')

    # if if the subdirs exist
    if not os.path.isdir(data_in):
        os.makedirs(data_in)

    if not os.path.isdir(data_in_retry):
        os.makedirs(data_in_retry)

    if not os.path.isdir(data_reject):
        os.makedirs(data_reject)

    # look for data in the data_in_retry and move it to data_in
    print " >> Moving files from data_in_retry to data_in. This process may take a while..."

    archive = pyArchiveStruct.RinexStruct(cnn)

    rfiles, paths, _ = archive.scan_archive_struct(data_in_retry)

    for rfile, path in zip(rfiles, paths):

        dest_file = os.path.join(data_in, rfile)

        if not os.path.isdir(os.path.dirname(dest_file)):
            os.makedirs(os.path.dirname(dest_file))

        # move the file into the folder
        shutil.move(path, dest_file)
        # remove folder from data_in_retry (also removes the log file)
        try:
            # remove the log file that accompanies this Z file
            os.remove(path.replace('d.Z', '.log'))
        except Exception:
            sys.exc_clear()

    remove_empty_folders(data_in_retry)

    # take a break to allow the FS to finish the task
    time.sleep(5)

    # delete any locks with a NetworkCode != '?%'
    cnn.query('delete from locks where "NetworkCode" not like \'?%\'')
    # get the locks to avoid reprocessing files that had no metadata in the database
    locks = cnn.query('SELECT * FROM locks')
    locks = locks.dictresult()

    files_path = []
    files_list = []
    tqdm.write("\n >> " + datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ": Starting repository recursive walk...")

    rpaths, _, files = archive.scan_archive_struct(data_in)

    for file, path in zip(files, rpaths):
        if path not in [lock['filename'] for lock in locks]:
            files_path.append(path)
            files_list.append(file)

    tqdm.write("Found %i files in the lock list..." % (len(locks)))
    tqdm.write("Found %i files to process..." % (len(files_list)))

    pbar = tqdm(total=len(files_path),ncols=80)

    # dependency functions
    depfuncs = (check_rinex_timespan_int, write_error, error_handle, insert_data, verify_rinex_multiday)
    # import modules
    modules = ('pyRinex', 'pyArchiveStruct', 'pyOTL', 'pyPPP', 'pyStationInfo', 'dbConnection', 'Utils', 'shutil', 'os',
            'uuid', 'datetime', 'pyDate', 'numpy', 'pySp3', 'traceback', 'platform', 'pyBrdc', 'pyOptions', 'pyEvents')

    callback = []

    for file_to_process, file in zip(files_path, files_list):

        if Config.run_parallel:

            arguments = (file_to_process, file, data_reject, data_in_retry)

            JobServer.SubmitJob(process_crinex_file, arguments, depfuncs, modules, callback, callback_class(pbar), 'callbackfunc')

            if JobServer.process_callback:
                # handle any output messages during this batch
                callback = output_handle(cnn, callback)
                JobServer.process_callback = False

        else:
            callback.append(callback_class(pbar))
            callback[0].callbackfunc(process_crinex_file(file_to_process, file, data_reject, data_in_retry))
            callback = output_handle(cnn, callback)

    # once we finnish walking the dir, wait and, handle the output messages
    if Config.run_parallel:
        tqdm.write(' >> waiting for jobs to finish...')
        JobServer.job_server.wait()
        tqdm.write(' >> Done.')

    # process the errors and the new stations
    output_handle(cnn, callback)

    pbar.close()

    # iterate to delete empty folders
    remove_empty_folders(data_in)

if __name__ == '__main__':

    main()
