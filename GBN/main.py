import random
import socket
import struct
import time
import os
import threading


BUFFER_SIZE = 4096
TIMEOUT = 10
WINDOW_SIZE = 3
LOSS_RATE = 0.1


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


class GBNSender:
    def __init__(self, senderSocket, address, timeout=TIMEOUT,
                 windowSize=WINDOW_SIZE, lossRate=LOSS_RATE):
        self.sender_socket = senderSocket
        self.timeout = timeout
        self.address = address
        self.window_size = windowSize
        self.loss_rate = lossRate
        self.send_base = 0
        self.next_seq = 0
        self.packets = [None] * 256

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
                #  返回值是一对（字节，地址）
                #  其中字节是代表接收到的数据的字节对象，而地址是发送数据的套接字的地址
                ack_seq, expect_seq = self.analyse_pkt(data)
                print('Sender receive ACK: ack_seq', ack_seq, "expect_seq", expect_seq)
                print("SEND WINDOW:", ack_seq)
                # 未检查 ACK 是否有损
                if self.send_base == (ack_seq + 1) % 256:
                    # 收到重复确认, 此处应当立即重发
                    pass
                self.send_base = max(self.send_base, (ack_seq + 1) % 256)
                if self.send_base == self.next_seq:  # 已发送分组确认完毕
                    self.sender_socket.settimeout(None)
                    return True
                else:
                    self.sender_socket.settimeout(self.timeout)
                    return True

            except socket.timeout:
                # 超时，重发分组.
                print('Sender wait for ACK timeout.')
                self.sender_socket.settimeout(self.timeout)  # reset timer
                for i in range(self.send_base, self.next_seq):
                    print('Sender resend packet:', i)
                    self.udp_send(self.packets[i])
                count += 1
        return False

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
        expect_seq = pkt[1]
        return ack_seq, expect_seq


class GBNReceiver:
    def __init__(self, receiverSocket, timeout=10, lossRate=0):
        self.receiver_socket = receiverSocket
        self.timeout = timeout
        self.loss_rate = lossRate
        self.expect_seq = 0
        self.target = None

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
                # 收到期望数据包且未出错
                if seq_num == self.expect_seq and getChecksum(data) == checksum:
                    self.expect_seq = (self.expect_seq + 1) % 256
                    ack_pkt = self.make_pkt(seq_num, self.expect_seq)
                    self.udp_send(ack_pkt)
                    if flag:    # 最后一个数据包
                        return data, True
                    else:
                        return data, False
                else:
                    ack_pkt = self.make_pkt((self.expect_seq - 1) % 256, self.expect_seq)
                    self.udp_send(ack_pkt)
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

    def make_pkt(self, ackSeq, expectSeq):
        """
        创建ACK确认报文
        """
        return struct.pack('BB', ackSeq, expectSeq)


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
        # 一次性发完一整个窗口内的分组并且等待对应序列号的ACK被返回
        while sender.next_seq < (sender.send_base + sender.window_size):
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

            if sender.send_base == sender.next_seq:
                sender.sender_socket.settimeout(sender.timeout)  # reset timer
            sender.next_seq = (sender.next_seq + 1) % 256
            pointer += 1
        sender.wait_ack()
        if pointer >= len(dataList):
            break
    fp.close()


def Receive(receiver, fp):
    while True:
        data, reset = receiver.wait_data()
        print('Data length:', len(data))
        fp.write(data)
        if reset:
            receiver.expect_seq = 0
            fp.close()
            break


# 双向传输实现

client_send_fp = open(os.path.dirname(__file__) + '/client/client_to_server.jpg', 'rb')
server_send_fp = open(os.path.dirname(__file__) + '/server/server_to_client.jpg', 'rb')
client_receive_fp = open(os.path.dirname(__file__) + '/client/' + str(int(time.time())) + '.jpg', 'ab')
server_receive_fp = open(os.path.dirname(__file__) + '/server/' + str(int(time.time())) + '.jpg', 'ab')

client_send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server_send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
client_receive_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server_receive_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

clientSender = GBNSender(client_send_socket, ('127.0.0.1', 8888))
serverSender = GBNSender(server_send_socket, ('127.0.0.1', 6666))
client_receive_socket.bind(('', 6666))
server_receive_socket.bind(('', 8888))
clientReceiver = GBNReceiver(client_receive_socket)
serverReceiver = GBNReceiver(server_receive_socket)

ClientSend = threading.Thread(target=Send, args=(clientSender, client_send_fp))
ServerSend = threading.Thread(target=Send, args=(serverSender, server_send_fp))
ClientReceive = threading.Thread(target=Receive, args=(clientReceiver, client_receive_fp))
ServerReceive = threading.Thread(target=Receive, args=(serverReceiver, server_receive_fp))

ClientReceive.start()
ServerReceive.start()
ClientSend.start()
ServerSend.start()

"""
# 模拟数据包丢失，单向传输更清晰
client_send_fp = open(os.path.dirname(__file__) + '/client/client_to_server.jpg', 'rb')
server_receive_fp = open(os.path.dirname(__file__) + '/server/' + str(int(time.time())) + '.jpg', 'ab')

client_send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server_receive_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
clientSender = GBNSender(client_send_socket, ('127.0.0.1', 8888))
server_receive_socket.bind(('', 8888))
serverReceiver = GBNReceiver(server_receive_socket)

ClientSend = threading.Thread(target=Send, args=(clientSender, client_send_fp))
ServerReceive = threading.Thread(target=Receive, args=(serverReceiver, server_receive_fp))

ServerReceive.start()
ClientSend.start()
"""
