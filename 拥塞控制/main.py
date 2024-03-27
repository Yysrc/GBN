import random
import socket
import struct
import time
import os
import threading

BUFFER_SIZE = 4096
TIMEOUT = 10
WINDOW_SIZE = 6
LOSS_RATE = 0.001


def getChecksum(data):
    """
    char_checksum 按字节计算校验和。每个字节被翻译为无符号整数
    @param data: 字节串
    """
    length = len(str(data))
    checksum = 0
    for i in range(0, length):
        checksum += int.from_bytes(bytes(str(data)[i], encoding='utf-8'), byteorder='little', signed=False)
        checksum &= 0xFF  # 强制截断
    return checksum


class SRSender:
    def __init__(self, senderSocket, address, timeout=TIMEOUT,
                 windowSize=WINDOW_SIZE, lossRate=LOSS_RATE):
        self.sender_socket = senderSocket
        self.timeout = timeout
        self.address = address
        self.cwnd = 1
        self.ssthresh = 8
        self.loss_rate = lossRate
        self.send_base = 0
        self.next_seq = 0
        self.packets = [None] * 256
        self.already_sent = [0] * 256
        self.received_ack = [0] * 256

    def udp_send(self, pkt):
        if self.loss_rate == 0 or random.randint(0, int(1 / self.loss_rate)) != 1:
            self.sender_socket.sendto(pkt, self.address)
        else:
            print('Packet lost.')
        time.sleep(0.2)

    def wait_ack(self):
        # self.sender_socket.settimeout(self.timeout)
        count = 0
        while True:
            if count >= 10:
                # 连续超时10次，接收方已断开，终止
                break
            try:
                data, address = self.sender_socket.recvfrom(BUFFER_SIZE)
                ack_seq = self.analyse_pkt(data)
                print('Sender receive ACK', ack_seq)
                # 慢启动和拥塞避免
                if self.cwnd < self.ssthresh:
                    self.cwnd *= 2
                print('CWND:', self.cwnd)

                if self.send_base <= ack_seq <= self.send_base+self.cwnd:
                    self.received_ack[ack_seq] += 1
                    # 快速重传
                    if self.received_ack[ack_seq] == 4:
                        self.udp_send(self.packets[ack_seq])
                        self.received_ack[ack_seq] = 1

                if self.send_base == ack_seq:
                    # 窗口序号向前移动到具有最小序号的未确认分组处
                    while self.received_ack[self.send_base] != 0:
                        self.send_base = (self.send_base + 1) % 256
                        print('SEND WINDOW move to:', self.send_base)
                if self.send_base == self.next_seq:
                    # 所有分组都已经确认，停止计时，cwnd值+1
                    self.sender_socket.settimeout(None)
                    self.cwnd += 1
                    print('CWND:', self.cwnd)
                    return

            except socket.timeout:
                # 超时，重发所有已发送但未收到确认的分组
                print('Sender wait for ACK timeout.')
                # 慢启动和拥塞避免
                self.ssthresh = self.cwnd // 2
                self.cwnd = 1
                print('CWND:', self.cwnd)

                for i in range(self.send_base, self.send_base + self.cwnd):
                    if self.already_sent[i] == 1 and self.received_ack[i] == 0:
                        print('Sender resend packet:', i)
                        self.udp_send(self.packets[i])
                self.sender_socket.settimeout(self.timeout)  # reset timer
                count += 1
        return

    def make_pkt(self, seqNum, data, checksum, stop=False):
        """
        将数据打包
        """
        flag = 1 if stop else 0
        return struct.pack('BBB', seqNum, flag, checksum) + data

    def analyse_pkt(self, pkt):
        """
        分析数据包
        """
        ack_seq = pkt[0]
        return ack_seq


class SRReceiver:
    def __init__(self, receiverSocket, timeout=10, lossRate=0, windowSize=WINDOW_SIZE):
        self.receiver_socket = receiverSocket
        self.timeout = timeout
        self.loss_rate = lossRate
        self.window_size = windowSize
        self.rcv_base = 0
        self.target = None
        self.received_data = [0] * 256
        self.buffer = [bytes('', encoding='utf-8')] * 256

    def udp_send(self, pkt):
        if self.loss_rate == 0 or random.randint(0, int(1 / self.loss_rate)) != 1:
            self.receiver_socket.sendto(pkt, self.target)
            print('Receiver send ACK:', pkt[0])
        else:
            print('Receiver send ACK:', pkt[0], ', but lost.')

    def wait_data(self):
        """
        接收方等待接受数据包
        """
        # self.receiver_socket.settimeout(self.timeout)
        while True:
            try:
                data, address = self.receiver_socket.recvfrom(BUFFER_SIZE)
                self.target = address
                seq_num, flag, checksum, data = self.analyse_pkt(data)
                print('Receiver receive packet:', seq_num)

                if self.rcv_base <= seq_num <= self.rcv_base+self.window_size-1 and getChecksum(data) == checksum:
                    # 一个选择ACK回送给发送方
                    ack_pkt = self.make_pkt(seq_num)
                    self.udp_send(ack_pkt)
                    # 快速重传
                    if seq_num > self.rcv_base:
                        ack_pkt = self.make_pkt(self.rcv_base)
                        self.udp_send(ack_pkt)

                    # 此分组之前未收到过，缓存此分组
                    if self.received_data[seq_num] == 0:
                        self.buffer[seq_num] = data
                    self.received_data[seq_num] = 1
                    # 此分组序号等于接收窗口的基序号，该分组以及之前缓存的序号连续的分组交付上层
                    if seq_num == self.rcv_base:
                        self.rcv_base = (self.rcv_base + 1) % 256  # 已经记录了data，需要将接收窗口滑动一步
                        for i in range(self.rcv_base, self.rcv_base + self.window_size):
                            if self.received_data[i] == 1:
                                data = data + self.buffer[i]
                                self.rcv_base = (self.rcv_base + 1) % 256  # 滑动接收窗口
                                print('RECEIVE WINDW move to:', self.rcv_base)
                            else:
                                break
                        if flag:
                            return data, True
                        else:
                            return data, False
                    return bytes('', encoding='utf-8'), False

                elif self.rcv_base-self.window_size <= seq_num <= self.rcv_base-1 and getChecksum(data) == checksum:
                    # 产生一个ack，即使该分组是接收方以前确认过的分组
                    ack_pkt = self.make_pkt(seq_num)
                    self.udp_send(ack_pkt)
                    return bytes('', encoding='utf-8'), False

                # 其他情况，忽略分组
                else:
                    return bytes('', encoding='utf-8'), False

            except socket.timeout:
                return bytes('', encoding='utf-8'), False

    def analyse_pkt(self, pkt):
        """
        分析数据包
        """
        seq_num = pkt[0]
        flag = pkt[1]
        checksum = pkt[2]
        data = pkt[3:]
        if flag == 0:
            print("seq_num: ", seq_num, "not end ")
        else:
            print("seq_num: ", seq_num, " end ")
        return seq_num, flag, checksum, data

    def make_pkt(self, ackSeq):
        """
        创建ACK确认报文
        """
        return struct.pack('B', ackSeq)


def Send(sender, fp):
    dataList = []
    while True:  # 把文件夹下的数据提取出来
        data = fp.read(2048)
        if len(data) <= 0:
            break
        dataList.append(data)
    print('The total number of data packets: ', len(dataList))
    pointer = 0
    while True:
        while sender.next_seq < (sender.send_base + sender.cwnd) \
                and sender.already_sent[sender.next_seq] == 0:
            if pointer >= len(dataList):
                break
            data = dataList[pointer]
            checksum = getChecksum(data)
            if pointer < len(dataList) - 1:
                sender.packets[sender.next_seq] = sender.make_pkt(sender.next_seq, data, checksum, stop=False)
            else:
                sender.packets[sender.next_seq] = sender.make_pkt(sender.next_seq, data, checksum, stop=True)
            print('Sender send packet:', pointer)
            sender.udp_send(sender.packets[sender.next_seq])
            sender.already_sent[sender.next_seq] = 1
            sender.next_seq = (sender.next_seq + 1) % 256
            pointer += 1
        sender.wait_ack()
        if pointer >= len(dataList):
            break
    fp.close()
    return


def Receive(receiver, fp):
    while True:
        data, reset = receiver.wait_data()
        print('Data length:', len(data))
        fp.write(data)
        if reset:
            receiver.rcv_base = 0
            fp.close()
            break

"""
client_send_fp = open(os.path.dirname(__file__) + '/client/client_to_server.jpg', 'rb')
server_send_fp = open(os.path.dirname(__file__) + '/server/server_to_client.jpg', 'rb')
client_receive_fp = open(os.path.dirname(__file__) + '/client/' + str(int(time.time())) + '.jpg', 'ab')
server_receive_fp = open(os.path.dirname(__file__) + '/server/' + str(int(time.time())) + '.jpg', 'ab')

client_send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server_send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
client_receive_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server_receive_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

clientSender = SRSender(client_send_socket, ('127.0.0.1', 8888))
serverSender = SRSender(server_send_socket, ('127.0.0.1', 6666))
client_receive_socket.bind(('', 6666))
server_receive_socket.bind(('', 8888))
clientReceiver = SRReceiver(client_receive_socket)
serverReceiver = SRReceiver(server_receive_socket)

ClientSend = threading.Thread(target=Send, args=(clientSender, client_send_fp))
ServerSend = threading.Thread(target=Send, args=(serverSender, server_send_fp))
ClientReceive = threading.Thread(target=Receive, args=(clientReceiver, client_receive_fp))
ServerReceive = threading.Thread(target=Receive, args=(serverReceiver, server_receive_fp))

ClientReceive.start()
ServerReceive.start()
ClientSend.start()
ServerSend.start()

"""
client_send_fp = open(os.path.dirname(__file__) + '/client/client_to_server.jpg', 'rb')
server_receive_fp = open(os.path.dirname(__file__) + '/server/' + str(int(time.time())) + '.jpg', 'ab')

client_send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server_receive_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
clientSender = SRSender(client_send_socket, ('127.0.0.1', 8888))
server_receive_socket.bind(('', 8888))
serverReceiver = SRReceiver(server_receive_socket)

ClientSend = threading.Thread(target=Send, args=(clientSender, client_send_fp))
ServerReceive = threading.Thread(target=Receive, args=(serverReceiver, server_receive_fp))

ServerReceive.start()
ClientSend.start()

