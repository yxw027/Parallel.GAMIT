"""
Project: Parallel.Archive
Date: 3/27/17 11:54 AM
Author: Demian D. Gomez

Integrity check utility of the database. Checks the following:
 - Station info consistency
 - Proposes a station info based on RINEX data
 - Searches for data gaps in the rinex table
 - Prints the station info records
 - renames or merges two stations into one
 usage
         --stn [net.]stn                    : Station to run integrity check on, comma separated stations allowed. If 'all', integrity check is run for all stations."
         --net network                      : Network of stations in --stn (if --stn is not in net.stn format). If --stn is not set, checks all stations in the network."
         --date StartDate[,EndDate]         : Date range to work on; can be yyyy/mm/dd or yyyy.doy or 'all'. If not specified, 'all' is assumed"
         --stninfo_rinex                    : Check that the receiver serial number in the rinex headers agrees with the station info receiver serial number. Output message if it doesn't."
         --stninfo_proposed [--ignore days] : Output a proposed station.info using the RINEX metadata. Optional, specify --ignore to ignore station.info records <= days."
         --stninfo                          : Check the consistency of the station information records in the database. Date range does not apply."
         --gaps [--ignore days]             : Check the RINEX files in the database and look for gaps (missing days). Optional, specify --ignore with the smallest gap to display."
         --spatial_coherence [--fix/del]    : Check that the RINEX files correspond to the stations they are linked to using their PPP coordinate."
                                              Add --fix to try to solve problems. In case the problem cannot be solved, add the RINEX file to the excluded table."
                                              Add --del to delete problems instead of moving data to the excluded table."
         --print_stninfo long|short         : Output the station info to stdout. long outputs the full line of the station info. short outputs a short version (better for screen visualization)."
         --rename [dest net].[dest stn]     : Takes the data from station --stn --net and renames (merges) it to [dest net].[dest stn]."
                                              It also changes the rinex filenames in the archive to match those of the new destiny station."
                                              If multiple stations are given as origins, all of them will be renamed as [dest net].[dest stn]."
                                              Limit the date range using the --date option"

"""

import sys
import getopt
import pyOptions
import dbConnection
import traceback
import pyDate
import pyStationInfo
import pyArchiveStruct
import pyPPP
import Utils
from tqdm import tqdm
import pp
import os
import numpy
import shutil
import time
import argparse
import pyScanArchive
from Utils import print_columns
from Utils import process_date
import pyJobServer
import platform
import pyEvents

class stninfo_rinex():

    def __init__(self,pbar):
        self.error = None
        self.pbar = pbar

    def callbackfunc(self, result):
        self.error = result[0]
        self.diff = result[1]
        self.pbar.update(1)

def compare_stninfo_rinex(NetworkCode, StationCode, STime, ETime, rinex_serial):

    try:
        cnn = dbConnection.Cnn("gnss_data.cfg")
    except:
        return (traceback.format_exc() + ' open de database when processing processing %s.%s' % (NetworkCode, StationCode), None)

    try:
        # get the center of the session
        date = STime + (ETime - STime)/2
        date = pyDate.Date(datetime=date)

        stninfo = pyStationInfo.StationInfo(cnn, NetworkCode, StationCode, date)

    except pyStationInfo.pyStationInfoException as e:
        return ("Station Information error: " + str(e), None)

    if stninfo.ReceiverSerial.lower() != rinex_serial.lower():
        return (None, [date, rinex_serial, stninfo.ReceiverSerial.lower()])

    return (None,None)


def get_differences(differences):

    err = [diff.error for diff in differences if diff.error]

    # print out any error messages
    for error in err:
        sys.stdout.write(error + '\n')

    return [diff.diff for diff in differences if not diff.diff is None]


def CheckRinexStn(NetworkCode, StationCode, start_date, end_date):

    # load the connection
    try:
        # try to open a connection to the database
        cnn = dbConnection.Cnn("gnss_data.cfg")
        Config = pyOptions.ReadOptions("gnss_data.cfg")
    except Exception:
        return (traceback.format_exc() + ' processing: (' + NetworkCode + ' ' + StationCode + ') using node ' + platform.node(), None)

    try:
        Archive = pyArchiveStruct.RinexStruct(cnn)

        rs = cnn.query('SELECT * FROM rinex WHERE "NetworkCode" = \'%s\' AND '
                       '"StationCode" = \'%s\' AND '
                       '"ObservationSTime" BETWEEN \'%s\' AND \'%s\' '
                       'ORDER BY "ObservationSTime"' % (NetworkCode, StationCode, start_date.yyyymmdd(), end_date.yyyymmdd()))

        rnxtbl = rs.dictresult()
        missing_files = []
        # response object for pyScanArchive --ppp
        response = []

        for rnx in rnxtbl:

            path = os.path.join(Config.archive_path, Archive.build_rinex_path(NetworkCode, StationCode, rnx['ObservationYear'], rnx['ObservationDOY'], with_filename=False))
            crinex_path = os.path.join(path, rnx['Filename'].replace(rnx['Filename'].split('.')[-1], rnx['Filename'].split('.')[-1].replace('o', 'd.Z')))

            if not os.path.exists(crinex_path):
                # problem with file! does not appear to be in the archive

                # any files to promote to rinex?
                rs = cnn.query('SELECT * FROM '
                               '(SELECT *, "ObservationETime" - "ObservationSTime" as timespan from rinex) as rinex '
                               'WHERE '
                               '"NetworkCode"     = \'%s\' AND '
                               '"StationCode"     = \'%s\' AND '
                               '"ObservationYear" = %i     AND '
                               '"ObservationDOY"  = %i '
                               'ORDER BY timespan DESC' % (NetworkCode, StationCode, rnx['ObservationYear'], rnx['ObservationDOY']))

                # get the dictionary of the results
                rnxe = rs.dictresult()

                # delete from ppp_soln table
                cnn.query('DELETE FROM ppp_soln WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "Year" = %i AND "DOY" = %i' % (rnx['NetworkCode'], rnx['StationCode'], rnx['ObservationYear'], rnx['ObservationDOY']))
                # delete from rinex table
                cnn.query('DELETE FROM rinex WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationYear" = %i AND "ObservationDOY" = %i' % (rnx['NetworkCode'], rnx['StationCode'], rnx['ObservationYear'], rnx['ObservationDOY']))

                found = False
                for rxe in rnxe:
                    crinex_path_r = os.path.join(path, rxe['Filename'].replace(rxe['Filename'].split('.')[-1], rxe['Filename'].split('.')[-1].replace('o', 'd.Z')))
                    # check if rinex extra exists
                    if os.path.exists(crinex_path_r):
                        cnn.begin_transac()
                        # move rinex_extra record to the the principal table
                        cnn.insert('rinex', rxe)
                        cnn.query('DELETE FROM rinex_extra WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationYear" = %i AND "ObservationDOY" = %i AND "Filename" = \'%s\'' % (rxe['NetworkCode'], rxe['StationCode'], rxe['ObservationYear'], rxe['ObservationDOY'], rxe['Filename']))

                        event = pyEvents.Event(Description='A missing RINEX file was found during RINEX integrity check ' + crinex_path + ' and was replaced with ' + crinex_path_r + '. A new PPP solution was executed for this file.',
                                               NetworkCode=NetworkCode,
                                               StationCode=StationCode,
                                               Year=rnx['ObservationYear'],
                                               DOY=rnx['ObservationDOY'])
                        cnn.insert_event(event)
                        cnn.commit_transac()
                        # sucess. Time to re-run the ppp solution for this station-day
                        resp = pyScanArchive.execute_ppp(rxe, crinex_path_r)
                        if resp:
                            response.append(resp)

                        found = True
                        break
                    else:
                        # non-existent rinex_extra, delete
                        cnn.query('DELETE FROM rinex_extra WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationYear" = %i AND "ObservationDOY" = %i AND "Filename" = \'%s\'' % (rxe['NetworkCode'], rxe['StationCode'], rxe['ObservationYear'], rxe['ObservationDOY'], rxe['Filename']))

                if not found:
                    event = pyEvents.Event(
                        Description='A missing RINEX file was found during RINEX integrity check ' + crinex_path + ' but it could not be replaced with a rinex_extra record.',
                        NetworkCode=NetworkCode,
                        StationCode=StationCode,
                        Year=rnx['ObservationYear'],
                        DOY=rnx['ObservationDOY'])

                    cnn.insert_event(event)

                missing_files += [crinex_path]

        # process the possible errors
        response_str = '\n'.join(response)
        if response_str == '':
            response_str = None

        return (response_str, missing_files)

    except Exception:
        return traceback.format_exc() + ' processing: ' + NetworkCode + ' ' + StationCode + ' using node ' + platform.node(), None


def CheckRinexIntegrity(stnlist, start_date, end_date, Config, JobServer):

    modules = ('os', 'pyArchiveStruct', 'dbConnection', 'pyOptions', 'traceback', 'pyScanArchive', 'platform', 'pyEvents')

    pbar = tqdm(total=len(stnlist),ncols=80)
    rinex_css = []

    for stn in stnlist:
        StationCode = stn['StationCode']
        NetworkCode = stn['NetworkCode']

        # distribute one station per job
        if Config.run_parallel:
            arguments = (NetworkCode, StationCode, start_date, end_date)

            JobServer.SubmitJob(CheckRinexStn, arguments, (), modules, rinex_css, stninfo_rinex(pbar), 'callbackfunc')

            if JobServer.process_callback:
                # collecting differences, nothing to be done!
                JobServer.process_callback = False
        else:
            rinex_css.append(stninfo_rinex(pbar))
            rinex_css[-1].callbackfunc(CheckRinexStn(NetworkCode, StationCode, start_date, end_date))

    if Config.run_parallel:
        JobServer.job_server.wait()

    pbar.close()

    for rinex in rinex_css:
        if rinex.diff:
            for diff in rinex.diff:
                print('File ' + diff + ' was not found in the archive. See events for details.')
        elif rinex.error:
            print('Error encountered: ' + rinex.error)


def StnInfoRinexIntegrity(cnn, stnlist, start_date, end_date, Config, JobServer):

    modules = ('pyStationInfo', 'dbConnection', 'pyDate', 'traceback')

    for stn in stnlist:

        StationCode = stn['StationCode']
        NetworkCode = stn['NetworkCode']

        rs = cnn.query('SELECT * FROM rinex WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationSTime" BETWEEN \'%s\' AND \'%s\' ORDER BY "ObservationSTime"' % (NetworkCode, StationCode, start_date.yyyymmdd(), end_date.yyyymmdd()))

        rnxtbl = rs.dictresult()

        tqdm.write('\nPerforming station info - rinex consistency check for %s.%s...' % (NetworkCode, StationCode),file=sys.stderr)
        maxlen = 0

        pbar = tqdm(total=rs.ntuples())
        differences = []

        for rnx in rnxtbl:

            if Config.run_parallel:
                arguments = (NetworkCode, StationCode, rnx['ObservationSTime'], rnx['ObservationETime'], rnx['ReceiverSerial'].lower())

                JobServer.SubmitJob(compare_stninfo_rinex, arguments, (), modules, differences, stninfo_rinex(pbar), 'callbackfunc')

                if JobServer.process_callback:
                    # collecting differences, nothing to be done!
                    JobServer.process_callback = False
            else:
                differences.append(stninfo_rinex(pbar))
                differences[-1].callbackfunc(compare_stninfo_rinex(NetworkCode, StationCode, rnx['ObservationSTime'], rnx['ObservationETime'], rnx['ReceiverSerial'].lower()))

        if Config.run_parallel:
            JobServer.job_server.wait()

        diff_vect = get_differences(differences)

        pbar.close()

        sys.stdout.write("\nStation info - rinex consistency check for %s.%s follows:\n" % (NetworkCode, StationCode))

        year = ''
        doy = ''
        print_head = True
        for i,diff in enumerate(diff_vect):
            year = Utils.get_norm_year_str(diff[0].year)
            doy = Utils.get_norm_doy_str(diff[0].doy)

            if print_head:
                sys.stdout.write('Warning! %s.%s from %s %s to ' % (NetworkCode,StationCode,year,doy))

            if i != len(diff_vect)-1:
                if diff_vect[i+1][0] == diff[0] + 1 and diff_vect[i+1][1] == diff[1] and diff_vect[i+1][2] == diff[2]:
                    print_head = False
                else:
                    sys.stdout.write('%s %s: RINEX SN %s != Station Information %s Possible change in station or bad RINEX metadata.\n' % (year, doy, diff_vect[i-1][1].ljust(maxlen), diff_vect[i-1][2].ljust(maxlen)))
                    print_head = True

        if diff_vect:
            sys.stdout.write('%s %s: RINEX SN %s != Station Information %s Possible change in station or bad RINEX metadata.\n' % (year, doy, diff_vect[-1][1].ljust(maxlen), diff_vect[-1][2].ljust(maxlen)))
        else:
            sys.stdout.write("No inconsistencies found.\n")


def StnInfoCheck(cnn, stnlist):
    # check that there are no inconsistencies in the station info records

    for stn in stnlist:
        NetworkCode = stn['NetworkCode']
        StationCode = stn['StationCode']

        first_obs = False
        try:
            stninfo = pyStationInfo.StationInfo(cnn,NetworkCode,StationCode) # type: pyStationInfo.StationInfo

            # there should not be more than one entry with 9999 999 in DateEnd
            empty_edata = [[record['DateEnd'],record['DateStart']] for record in stninfo.records if not record['DateEnd']]

            if len(empty_edata) > 1:
                list_empty = [pyDate.Date(datetime=record[1]).yyyyddd() for record in empty_edata]
                list_empty = ', '.join(list_empty)
                sys.stdout.write('There is more than one station info entry with Session Stop = 9999 999 Session Start -> %s\n' % (list_empty))

            # there should not be a DateStart < DateEnd of different record
            list_problems = []

            for i,record in enumerate(stninfo.records):
                overlaps = stninfo.overlaps(record)
                if overlaps:

                    for overlap in overlaps:
                        if overlap['DateStart'] != record['DateStart']:
                            ds1, de1 = stninfo.datetime2stninfodate(record['DateStart'], record['DateEnd'])
                            ds2, de2 = stninfo.datetime2stninfodate(overlap['DateStart'], overlap['DateEnd'])

                            list_problems.append([ds2, de2, ds1, de1])

            # there should not be RINEX data outside the station info window
            rs = cnn.query('SELECT min("ObservationSTime") as first_obs, max("ObservationSTime") as last_obs FROM rinex WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\'' % (NetworkCode, StationCode))

            rnxtbl = rs.dictresult()

            if rnxtbl[0]['first_obs'] is not None:
                # to avoid empty stations (no rinex data)
                if rnxtbl[0]['first_obs'] < stninfo.records[0]['DateStart']:
                    d1 = pyDate.Date(datetime=rnxtbl[0]['first_obs'])
                    d2 = pyDate.Date(datetime=stninfo.records[0]['DateStart'])
                    sys.stdout.write('There is one or more RINEX observation file(s) outside the Session Start -> RINEX: %s STNINFO: %s\n' % (d1.yyyyddd(), d2.yyyyddd()))
                    first_obs = True

                if stninfo.records[-1]['DateEnd'] is not None and rnxtbl[0]['last_obs'] > stninfo.records[-1]['DateEnd']:
                    d1 = pyDate.Date(datetime=rnxtbl[0]['last_obs'])
                    d2 = pyDate.Date(datetime=stninfo.records[-1]['DateEnd'])
                    sys.stdout.write('There is one or more RINEX observation file(s) outside the last Session End -> RINEX: %s STNINFO: %s\n' % (d1.yyyyddd(), d2.yyyyddd()))
                    first_obs = True

            if len(list_problems) > 0:
                list_problems = [record[0] + ' -> ' + record[1] + ' conflicts ' + record[2] + ' -> ' + record[3] for record in list_problems]
                list_problems = '\n   '.join(list_problems)
                sys.stdout.write('There are conflicting recods in the station information table for %s.%s.\n   %s\n' % (NetworkCode, StationCode, list_problems))

            if len(empty_edata) > 1 or len(list_problems) > 0 or first_obs:
                # only print a partial of the station info:
                stninfo_lines = stninfo.return_stninfo().split('\n')
                stninfo_lines = [' ' + NetworkCode.upper() + '.' + line[1:110] + ' [...] ' + line[160:] for line in stninfo_lines]

                sys.stdout.write('\n'.join(stninfo_lines) + '\n\n')
            else:
                sys.stderr.write('No problems found for %s.%s\n' % (NetworkCode, StationCode))

            sys.stdout.flush()

        except pyStationInfo.pyStationInfoException as e:
            tqdm.write(str(e))

def ecef2lla(ecefArr):
    # convert ECEF coordinates to LLA
    # test data : test_coord = [2297292.91, 1016894.94, -5843939.62]
    # expected result : -66.8765400174 23.876539914 999.998386689

    x = float(ecefArr[0])
    y = float(ecefArr[1])
    z = float(ecefArr[2])

    a = 6378137
    e = 8.1819190842622e-2

    asq = numpy.power(a, 2)
    esq = numpy.power(e, 2)

    b = numpy.sqrt(asq * (1 - esq))
    bsq = numpy.power(b, 2)

    ep = numpy.sqrt((asq - bsq) / bsq)
    p = numpy.sqrt(numpy.power(x, 2) + numpy.power(y, 2))
    th = numpy.arctan2(a * z, b * p)

    lon = numpy.arctan2(y, x)
    lat = numpy.arctan2((z + numpy.power(ep, 2) * b * numpy.power(numpy.sin(th), 3)),
                     (p - esq * a * numpy.power(numpy.cos(th), 3)))
    N = a / (numpy.sqrt(1 - esq * numpy.power(numpy.sin(lat), 2)))
    alt = p / numpy.cos(lat) - N

    lon = lon * 180 / numpy.pi
    lat = lat * 180 / numpy.pi

    return numpy.array([lat]), numpy.array([lon]), numpy.array([alt])


def CheckSpatialCoherence(cnn, stnlist, start_date, end_date):

    for stn in stnlist:

        NetworkCode = stn['NetworkCode']
        StationCode = stn['StationCode']

        rs = cnn.query(
            'SELECT * FROM ppp_soln WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "Year" || \' \' || "DOY" BETWEEN \'%s\' AND \'%s\'' % (NetworkCode, StationCode, start_date.yyyy() + ' ' + start_date.ddd(), end_date.yyyy() + ' ' + end_date.ddd()))

        ppp = rs.dictresult()

        if rs.ntuples() > 0:
            tqdm.write('\nChecking spatial coherence of PPP solutions for %s.%s...' % (NetworkCode,StationCode),sys.stderr)

            for soln in tqdm(ppp):
                year = soln['Year']
                doy = soln['DOY']
                date = pyDate.Date(year=year,doy=doy)

                # calculate lla of solution
                lat, lon, h = ecef2lla([float(soln['X']), float(soln['Y']), float(soln['Z'])])

                SpatialCheck = pyPPP.PPPSpatialCheck(lat, lon, h)

                Result, match, closest_stn = SpatialCheck.verify_spatial_coherence(cnn, StationCode)

                if Result:

                    if len(closest_stn) == 0:
                        if match['NetworkCode'] != NetworkCode and match['StationCode'] != StationCode:
                            # problem! we found a station but does not agree with the declared solution name
                            tqdm.write('Warning! Solution for %s.%s %s is a match for %s.%s (only this candidate was found).\n' % (NetworkCode,StationCode,date.yyyyddd(),match['NetworkCode'],match['StationCode']))
                    else:
                        tqdm.write(
                            'Wanrning! Solution for %s.%s %s was found to be closer to %s.%s. Distance to %s.%s: %.3f m. Distance to %s.%s: %.3f m' % (
                            NetworkCode, StationCode, date.yyyyddd(), closest_stn['NetworkCode'], closest_stn['StationCode'],
                            closest_stn['NetworkCode'], closest_stn['StationCode'], closest_stn['distance'],
                            match['NetworkCode'], match['StationCode'], match['distance']))

                else:
                    if len(match) > 0:
                        # No name match but there are candidates for the station
                        matches = ', '.join(['%s.%s: %.3f m' % (m['NetworkCode'], m['StationCode'], m['distance']) for m in match])
                        tqdm.write('Wanrning! Solution for %s.%s %s does not match its station code. Cantidates and distances found: %s' % (NetworkCode, StationCode, date.yyyyddd(),matches))
                    else:
                        tqdm.write(
                            'Wanrning! PPP for %s.%s %s had no match within 100 m. Closest station is %s.%s (%3.f km, %s.%s %s PPP solution is: %.8f %.8f)' % (
                            NetworkCode, StationCode, date.yyyyddd(), closest_stn['NetworkCode'], closest_stn['StationCode'],
                            numpy.divide(float(closest_stn['distance']), 1000), NetworkCode, StationCode, date.yyyyddd(),lat[0], lon[0]))
        else:
            tqdm.write('\nNo PPP solutions found for %s.%s...' % (NetworkCode, StationCode), sys.stderr)

def GetStnGaps(cnn, stnlist, ignore_val, start_date, end_date):

    for stn in stnlist:
        NetworkCode = stn['NetworkCode']
        StationCode = stn['StationCode']

        rs = cnn.query(
            'SELECT * FROM rinex WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationSTime" BETWEEN \'%s\' AND \'%s\' ORDER BY "ObservationSTime"' % (NetworkCode, StationCode, start_date.yyyymmdd(), end_date.yyyymmdd()))

        rnxtbl = rs.dictresult()
        gap_begin = None
        gaps = []
        for i, rnx in enumerate(rnxtbl):

            if i > 0:
                d1 = pyDate.Date(year=rnx['ObservationYear'],doy=rnx['ObservationDOY'])
                d2 = pyDate.Date(year=rnxtbl[i-1]['ObservationYear'],doy=rnxtbl[i-1]['ObservationDOY'])

                if d1 != d2 + 1 and not gap_begin:
                    gap_begin = d2 + 1

                if d1 == d2 + 1 and gap_begin:
                    days = ((d2-1).mjd - gap_begin.mjd)+1
                    if days > ignore_val:
                        gaps.append('%s.%s gap in data found %s -> %s (%i days)' % (NetworkCode,StationCode,gap_begin.yyyyddd(),(d2-1).yyyyddd(), days))

                    gap_begin = None

        if gaps:
            sys.stdout.write('\nData gaps in %s.%s follow:\n' % (NetworkCode, StationCode))
            sys.stdout.write('\n'.join(gaps) + '\n')
        else:
            sys.stdout.write('\nNo data gaps found for %s.%s\n' % (NetworkCode, StationCode))


def PrintStationInfo(cnn, stnlist, short=False):

    for stn in stnlist:
        NetworkCode = stn['NetworkCode']
        StationCode = stn['StationCode']

        try:
            stninfo = pyStationInfo.StationInfo(cnn,NetworkCode,StationCode)

            stninfo_lines = stninfo.return_stninfo().split('\n')

            if short:
                stninfo_lines = [' ' + NetworkCode.upper() + '.' + line[1:110] + ' [...] ' + line[160:] for line in stninfo_lines]
                sys.stdout.write('\n'.join(stninfo_lines) + '\n\n')
            else:
                stninfo_lines = [line for line in stninfo_lines]
                sys.stdout.write('# ' + NetworkCode.upper() + '.' + StationCode.upper() + '\n' + '\n'.join(stninfo_lines) + '\n')

        except pyStationInfo.pyStationInfoException as e:
            sys.stdout.write(str(e))

def RenameStation(cnn, NetworkCode, StationCode, DestNetworkCode, DestStationCode, start_date, end_date, archive_path):

    # make sure the destiny station exists
    try:
        rs = cnn.query('SELECT * FROM stations WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\'' % (DestNetworkCode, DestStationCode))

        if rs.ntuples() == 0:
            # ask the user if he/she want to create it?
            print 'The requested destiny station does not exist. Please create it and try again'
        else:

            # select the original rinex files names
            # this is the set that will effectively be transferred to the dest net and stn codes
            # I select this portion of data here and not after we rename the station to prevent picking up more data
            # (due to the time window) that is ALREADY in the dest station.
            rs = cnn.query(
                'SELECT * FROM rinex WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationSTime" BETWEEN \'%s\' AND \'%s\'' % (
                    NetworkCode, StationCode, start_date.yyyymmdd(), end_date.yyyymmdd()))

            original_rs = rs.dictresult()

            print " >> Beginning transfer of %i rinex files from %s.%s to %s.%s" % (len(original_rs), NetworkCode, StationCode, DestNetworkCode, DestStationCode)

            for src_rinex in tqdm(original_rs):
                # rename files
                Archive = pyArchiveStruct.RinexStruct(cnn) # type: pyArchiveStruct.RinexStruct
                src_file_path = Archive.build_rinex_path(NetworkCode, StationCode, src_rinex['ObservationYear'], src_rinex['ObservationDOY'])

                src_path = os.path.split(os.path.join(archive_path, src_file_path))[0]
                src_file = os.path.split(os.path.join(archive_path, src_file_path))[1]

                dest_file = src_file.replace(StationCode, DestStationCode)

                cnn.begin_transac()

                # update the NetworkCode and StationCode and filename information in the db
                cnn.query(
                    'UPDATE rinex SET "NetworkCode" = \'%s\', "StationCode" = \'%s\', "Filename" = \'%s\' '
                    'WHERE "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' AND "ObservationYear" = %i AND "ObservationDOY" = %i'
                    % (DestNetworkCode, DestStationCode, dest_file.replace('d.Z','o'), NetworkCode, StationCode, src_rinex['ObservationYear'], src_rinex['ObservationDOY']))

                # DO NOT USE pyArchiveStruct because we have an active transaction and the change is not visible yet
                # because we don't know anything about the archive's stucture, we just try to replace the names and that should suffice
                dest_path = src_path.replace(StationCode, DestStationCode).replace(NetworkCode, DestNetworkCode)

                # check that the destination path exists (it should, but...)
                if not os.path.isdir(dest_path):
                    os.makedirs(dest_path)

                shutil.move(os.path.join(src_path, src_file), os.path.join(dest_path, dest_file))

                # if we are here, we are good. Commit
                cnn.commit_transac()

                date = pyDate.Date(year=src_rinex['ObservationYear'], doy=src_rinex['ObservationDOY'])
                # Station info transfer
                try:
                    stninfo_dest = pyStationInfo.StationInfo(cnn, DestNetworkCode, DestStationCode, date) # type: pyStationInfo.StationInfo
                    # no error, nothing to do.
                except pyStationInfo.pyStationInfoException:
                    # failed to get a valid station info record! we need to incorporate the station info record from
                    # the source station
                    try:
                        stninfo_dest = pyStationInfo.StationInfo(cnn, DestNetworkCode, DestStationCode)  # type: pyStationInfo.StationInfo
                        stninfo_src = pyStationInfo.StationInfo(cnn, NetworkCode, StationCode, date) # type: pyStationInfo.StationInfo

                        # force the station code in record to be the same as deststationcode
                        record = stninfo_src.currentrecord
                        record['StationCode'] = DestStationCode

                        stninfo_dest.InsertStationInfo(record)

                    except pyStationInfo.pyStationInfoException as e:
                        # if there is no station info for this station either, warn the user!
                        tqdm.write(' -- Error while updating Station Information! %s' % (str(e)))


    except:
        cnn.rollback_transac()
        raise

def print_help():
    print "  usage: "
    print "         --stn [net.]stn                    : Station to run integrity check on, comma separated stations allowed. If 'all', integrity check is run for all stations."
    print "         --net network                      : Network of stations in --stn (if --stn is not in net.stn format). If --stn is not set, checks all stations in the network."
    print "         --date StartDate[,EndDate]         : Date range to work on; can be yyyy/mm/dd or yyyy.doy or 'all'. If not specified, 'all' is assumed"
    print "         --stninfo_rinex                    : Check that the receiver serial number in the rinex headers agrees with the station info receiver serial number. Output message if it doesn't."
    print "         --stninfo_proposed [--ignore days] : Output a proposed station.info using the RINEX metadata. Optional, specify --ignore to ignore station.info records <= days."
    print "         --stninfo                          : Check the consistency of the station information records in the database. Date range does not apply."
    print "         --gaps [--ignore days]             : Check the RINEX files in the database and look for gaps (missing days). Optional, specify --ignore with the smallest gap to display."
    print "         --spatial_coherence [--fix/del]    : Check that the RINEX files correspond to the stations they are linked to using their PPP coordinate."
    print "                                              Add --fix to try to solve problems. In case the problem cannot be solved, add the RINEX file to the excluded table."
    print "                                              Add --del to delete problems instead of moving data to the excluded table."
    print "         --print_stninfo long|short         : Output the station info to stdout. long outputs the full line of the station info. short outputs a short version (better for screen visualization)."
    print "         --rename [dest net].[dest stn]     : Takes the data from station --stn --net and renames (merges) it to [dest net].[dest stn]."
    print "                                              It also changes the rinex filenames in the archive to match those of the new destiny station."
    print "                                              If multiple stations are given as origins, all of them will be renamed as [dest net].[dest stn]."
    print "                                              Limit the date range using the --date option"

def parse_input(argv, cnn):

    stn_list = ''
    net_list = ''
    ignore_val = 0
    run_stninfo_rinex_check = False
    run_stninfo_check = False
    run_spatial_coherence = False
    run_gaps = False
    run_print_stninfo_long = False
    run_print_stninfo_short = False
    run_rename_station = False
    run_proposed = False
    no_parallel = False
    start_date = None
    end_date = None

    DestNetworkCode = None
    DestStationCode = None

    try:
        aoptions, arguments = getopt.getopt(argv, '',
                                            ['stn=', 'net=', 'date=', 'stninfo_rinex', 'stninfo', 'gaps', 'ignore=',
                                             'spatial_coherence', 'print_stninfo=', 'stninfo_proposed',
                                             'rename=','noparallel'])
    except getopt.GetoptError:
        print "invalid argument/s"
        print_help()
        sys.exit(2)

    for opt, args in aoptions:
        if opt == '--stn':
            if ',' in args:
                stn_list = args.lower().split(',')
            elif 'all' in args:
                stn_list = get_all_stations(cnn)
            else:
                stn_list = [args.lower()]
        if opt == '--net':
            net_list = args.lower()

        elif opt == '--noparallel':
            no_parallel = True

        if opt == '--date':
            if ',' in args:
                sdate = args.split(',')[0]
                edate = args.split(',')[1]

                if '/' in args:
                    start_date = pyDate.Date(year=sdate.split('/')[0], month=sdate.split('/')[1],
                                             day=sdate.split('/')[2])
                    end_date = pyDate.Date(year=edate.split('/')[0], month=edate.split('/')[1],
                                           day=edate.split('/')[2])
                elif '.' in args:
                    start_date = pyDate.Date(year=sdate.split('.')[0], doy=sdate.split('.')[1])
                    end_date = pyDate.Date(year=edate.split('.')[0], doy=edate.split('.')[1])

            elif 'all' in args:
                start_date = pyDate.Date(year=1900, month=1, day=1)
                end_date = pyDate.Date(year=2100, month=1, day=1)
            elif '/' in args:
                # single date, assume start only
                start_date = pyDate.Date(year=args.split('/')[0], month=args.split('/')[1], day=args.split('/')[2])
                end_date = pyDate.Date(year=2100, month=1, day=1)
            elif '.' in args:
                start_date = pyDate.Date(year=args.split('.')[0], doy=args.split('.')[1])
                end_date = pyDate.Date(year=2100, month=1, day=1)
            else:
                print "Invalid date format!"
                print_help()
                exit(2)

        if opt == '--stninfo_rinex':
            run_stninfo_rinex_check = True

        if opt == '--stninfo':
            run_stninfo_check = True

        if opt == '--gaps':
            run_gaps = True

        if opt == '--stninfo_proposed':
            run_proposed = True

        if opt == '--ignore':
            try:
                ignore_val = int(args)
            except:
                print "invalid ignore interval"
                exit(2)

        if opt == '--spatial_coherence':
            run_spatial_coherence = True

        if opt == '--print_stninfo':
            if args == 'short':
                run_print_stninfo_short = True
            elif args == 'long':
                run_print_stninfo_long = True
            else:
                print "Invalid --print_stninfo argument: must select between short or long"
                print_help()
                exit(2)

        if opt == '--rename':
            run_rename_station = True
            try:
                DestNetworkCode = args.lower().split('.')[0]
                DestStationCode = args.lower().split('.')[1]
            except:
                print "invalid destiny station"
                exit(2)

    if stn_list == '' and net_list == '':
        print 'Invalid input, must specify at least one: --stn --net'

    if stn_list == '':
        stn_list = get_all_stations(cnn, net_list)

    if net_list == '':
        for stn in stn_list:
            if not '.' in stn:
                print "No --net specified. All stations must be in net.stn format"
                print_help()
                exit(2)

    if not start_date:
        start_date = pyDate.Date(year=1900, month=1, day=1)
        end_date = pyDate.Date(year=2100, month=1, day=1)

    return stn_list, net_list, start_date, end_date, run_stninfo_rinex_check, run_stninfo_check, \
           run_gaps, ignore_val, run_spatial_coherence, run_print_stninfo_short, run_print_stninfo_long, \
           run_proposed, run_rename_station, DestNetworkCode, DestStationCode, no_parallel

def get_all_stations(cnn, NetworkCode=''):

    if NetworkCode == '':
        rs = cnn.query('SELECT "NetworkCode" || \'.\' || "StationCode" as stn FROM stations WHERE "NetworkCode" not like \'?%%\' ORDER BY "NetworkCode", "StationCode"')
    else:
        rs = cnn.query('SELECT "NetworkCode" || \'.\' || "StationCode" as stn FROM stations WHERE "NetworkCode" = \'%s\' ORDER BY "NetworkCode", "StationCode"' % (NetworkCode))

    stns = rs.dictresult()

    return [stn.get('stn') for stn in stns]

def main():

    parser = argparse.ArgumentParser(description='Database integrity tools, metadata check and fixing tools program')

    parser.add_argument('stnlist', type=str, nargs='+', metavar='all|net.stnm',
                        help="List of networks/stations to process given in [net].[stnm] format or just [stnm] (separated by spaces; if [stnm] is not unique in the database, all stations with that name will be processed). Use keyword 'all' to process all stations in the database. If [net].all is given, all stations from network [net] will be processed. Alternatevily, a file with the station list can be provided.")

    parser.add_argument('-d', '--date_filter', nargs='+', metavar='date', help='Date range filter for all operations. Can be specified in yyyy/mm/dd or yyyy.doy format')
    parser.add_argument('-rinex', '--check_rinex', action='store_true', help='Check the RINEX integrity of the archive-database by verifying that the RINEX files reported in rinex and rinex_extra exist in the archive. If a RINEX file does not exist, remove the record and promote a rinex_extra file to the rinex table (if exists). PPP record is deleted and a new PPP coordinates is calculated for the promoted file.')
    parser.add_argument('-stnr', '--station_info_rinex', action='store_true', help='Check that the receiver serial number in the rinex headers agrees with the station info receiver serial number.')
    parser.add_argument('-stns', '--station_info_solutions', action='store_true', help='Check that the PPP hash values match the station info hash.')
    parser.add_argument('-stnp', '--station_info_proposed', metavar='ignore_days', const=0, type=int, nargs='?', help='Output a proposed station.info using the RINEX metadata. Optional, specify [ignore_days] to ignore station.info records <= days.')
    parser.add_argument('-stnc', '--station_info_check', action='store_true', help='Check the consistency of the station information records in the database. Date range does not apply. Also, check that the RINEX files fall within a valid station information record.')
    parser.add_argument('-g', '--data_gaps', metavar='ignore_days', const=0, type=int, nargs='?', help='Check the RINEX files in the database and look for gaps (missing days). Optional, [ignore_days] with the smallest gap to display.')
    parser.add_argument('-sc', '--spatial_coherence', choices=['exclude', 'delete', 'noop'], type=str, nargs=1, help='Check that the RINEX files correspond to the stations they are linked to using their PPP coordinate. If keyword [exclude] or [delete], add the PPP solution to the excluded table or delete the PPP solution. If [noop], then only report but do not exlude or delete.')
    parser.add_argument('-print', '--print_stninfo', choices=['long', 'short'], type=str, nargs=1, help='Output the station info to stdout. [long] outputs the full line of the station info. [short] outputs a short version (better for screen visualization).')
    parser.add_argument('-r', '--rename', metavar='net.stnm', nargs=1, help="Takes the data from the station list and renames (merges) it to net.stnm."
                                                         "It also changes the rinex filenames in the archive to match those of the new destiny station. "
                                                         "Only a single station can be given as the origin and destiny. "
                                                         "Limit the date range using the -d option.")
    parser.add_argument('-np', '--noparallel', action='store_true', help="Execute command without parallelization.")

    args = parser.parse_args()

    cnn = dbConnection.Cnn("gnss_data.cfg") # type: dbConnection.Cnn

    # create the execution log
    cnn.insert('executions', script='pyIntegrityCheck.py')

    Config = pyOptions.ReadOptions("gnss_data.cfg") # type: pyOptions.ReadOptions

    if len(args.stnlist) == 1 and os.path.isfile(args.stnlist[0]):
        print ' >> Station list read from ' + args.stnlist[0]
        stnlist = [line.strip() for line in open(args.stnlist[0], 'r')]
        stnlist = [{'NetworkCode': item.split('.')[0], 'StationCode': item.split('.')[1]} for item in stnlist]
    else:
        stnlist = Utils.process_stnlist(cnn, args.stnlist)

    print ' >> Selected station list:'
    print_columns([item['NetworkCode'] + '.' + item['StationCode'] for item in stnlist])

    if not args.noparallel:
        JobServer = pyJobServer.JobServer(Config) # type: pyJobServer.JobServer
    else:
        JobServer = None
        Config.run_parallel = False

    #####################################
    # date filter

    dates = [pyDate.Date(year=1980, doy=1), pyDate.Date(year=2100, doy=1)]

    if args.date_filter:
        for i, arg in enumerate(args.date_filter):
            try:
                dates[i] = process_date(arg)
            except Exception as e:
                parser.error('Error while reading the date start/end parameters: ' + str(e) + '\n' + traceback.format_exc())

    #####################################

    if args.check_rinex:
        CheckRinexIntegrity(stnlist, dates[0], dates[1], Config, JobServer)

    #####################################

    if args.station_info_rinex:

        StnInfoRinexIntegrity(cnn, stnlist, dates[0], dates[1], Config, JobServer)

    #####################################

    if args.station_info_check:
        StnInfoCheck(cnn, stnlist)

    #####################################

    if args.data_gaps is not None:
        GetStnGaps(cnn, stnlist, args.data_gaps, dates[0], dates[1])

    #####################################

    if args.spatial_coherence is not None:
        CheckSpatialCoherence(cnn, stnlist, dates[0], dates[1])

    #####################################

    if args.print_stninfo is not None:
        if args.print_stninfo[0] == 'short':
            PrintStationInfo(cnn, stnlist, True)
        elif args.print_stninfo[0] == 'long':
            PrintStationInfo(cnn, stnlist, False)
        else:
            parser.error('Argument for print_stninfo has to be either long or short')

    #####################################

    if args.station_info_proposed is not None:
        for stn in stnlist:
            stninfo = pyStationInfo.StationInfo(cnn, stn['NetworkCode'], stn['StationCode'])
            sys.stdout.write(stninfo.rinex_based_stninfo(args.station_info_proposed))

    #####################################

    if args.rename:
        if len(stnlist) > 1:
            parser.error('Only a single station should be given for the origin station')

        if not '.' in args.rename[0]:
            parser.error('Format for destiny station should be net.stnm')
        else:
            DestNetworkCode = args.rename[0].split('.')[0]
            DestStationCode = args.rename[0].split('.')[1]

            RenameStation(cnn, stnlist[0]['NetworkCode'], stnlist[0]['StationCode'], DestNetworkCode, DestStationCode, dates[0], dates[1], Config.archive_path)


if __name__ == '__main__':

    main()
