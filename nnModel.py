import torch
import torch.nn as nn
import time
from torch.autograd import Variable
import random


DROP_OUT = 0.5

QPM_INDEX = 0
# VOICE_IDX = 11
TEMPO_IDX = 25
PITCH_IDX = 12
QPM_PRIMO_IDX = 4
TEMPO_PRIMO_IDX = -2
GRAPH_KEYS = ['onset', 'forward', 'melisma', 'rest', 'slur']
N_EDGE_TYPE = len(GRAPH_KEYS) * 2
NUM_VOICE_FEED_PARAM = 2

class GatedGraph(nn.Module):
    def  __init__(self, size, num_edge_style, device, secondary_size=0):
        super(GatedGraph, self).__init__()
        if secondary_size == 0:
            secondary_size = size
        # for i in range(num_edge_style):
        #     subgraph = self.subGraph(size)
        #     self.sub.append(subgraph)
        self.size = size
        self.secondary_size = secondary_size

        self.wz = torch.nn.Parameter(torch.Tensor(num_edge_style,size,secondary_size))
        self.wr = torch.nn.Parameter(torch.Tensor(num_edge_style,size,secondary_size))
        self.wh = torch.nn.Parameter(torch.Tensor(num_edge_style,size,secondary_size))
        self.uz = torch.nn.Parameter(torch.Tensor(size, secondary_size))
        self.bz = torch.nn.Parameter(torch.Tensor(secondary_size))
        self.ur = torch.nn.Parameter(torch.Tensor(size, secondary_size))
        self.br = torch.nn.Parameter(torch.Tensor(secondary_size))
        self.uh = torch.nn.Parameter(torch.Tensor(secondary_size, secondary_size))
        self.bh = torch.nn.Parameter(torch.Tensor(secondary_size))

        nn.init.xavier_normal_(self.wz)
        nn.init.xavier_normal_(self.wr)
        nn.init.xavier_normal_(self.wh)
        nn.init.xavier_normal_(self.uz)
        nn.init.xavier_normal_(self.ur)
        nn.init.xavier_normal_(self.uh)
        nn.init.zeros_(self.bz)
        nn.init.zeros_(self.br)
        nn.init.zeros_(self.bh)

        self.sigmoid = torch.nn.Sigmoid()
        self.tanh = torch.nn.Tanh()

    def forward(self, input, edge_matrix, iteration=10):
        for i in range(iteration):
            activation = torch.matmul(edge_matrix.transpose(1,2), input)
            temp_z = self.sigmoid( torch.bmm(activation, self.wz).sum(0) + torch.matmul(input, self.uz) + self.bz)
            temp_r = self.sigmoid( torch.bmm(activation, self.wr).sum(0) + torch.matmul(input, self.ur) + self.br)

            if self.secondary_size == self.size:
                temp_hidden = self.tanh(
                    torch.bmm(activation, self.wh).sum(0) + torch.matmul(temp_r * input, self.uh) + self.bh)
                input = (1 - temp_z) * input + temp_r * temp_hidden
            else:
                temp_hidden = self.tanh(
                    torch.bmm(activation, self.wh).sum(0) + torch.matmul(temp_r * input[:,:,-self.secondary_size:], self.uh) + self.bh)
                temp_result = (1 - temp_z) * input[:,:,-self.secondary_size:] + temp_r * temp_hidden
                input = torch.cat((input[:,:,:-self.secondary_size], temp_result), 2)

        return input


class GGNN_HAN(nn.Module):
    def __init__(self, network_parameters, device, LOSS_TYPE, tempo_length=1, num_trill_param=5):
        super(GGNN_HAN, self).__init__()
        self.device = device
        self.loss_type = LOSS_TYPE
        self.tempo_output_length = tempo_length

        self.input_size = network_parameters.input_size
        self.output_size = network_parameters.output_size
        self.num_layers = network_parameters.note.layer
        self.note_hidden_size = network_parameters.note.size
        self.num_beat_layers = network_parameters.beat.layer
        self.beat_hidden_size = network_parameters.beat.size
        self.num_measure_layers = network_parameters.measure.layer
        self.measure_hidden_size = network_parameters.measure.size
        self.final_hidden_size = network_parameters.final.size
        self.num_voice_layers = network_parameters.voice.layer
        self.voice_hidden_size = network_parameters.voice.size
        self.final_input = network_parameters.final.input
        self.encoder_size = network_parameters.encoder.size
        self.encoder_input_size = network_parameters.encoder.input
        self.encoder_layer_num = network_parameters.encoder.layer
        self.onset_hidden_size = network_parameters.onset.size
        self.num_onset_layers = network_parameters.onset.layer
        self.num_edge_types = network_parameters.num_edge_types
        self.graph_iteration = network_parameters.graph_iteration


        self.beat_attention = nn.Linear(self.note_hidden_size * 2, self.note_hidden_size * 2)
        self.beat_rnn = nn.LSTM(self.note_hidden_size * 2, self.beat_hidden_size, self.num_beat_layers, batch_first=True, bidirectional=True, dropout=DROP_OUT)
        self.measure_attention = nn.Linear(self.beat_hidden_size*2, self.beat_hidden_size*2)
        self.measure_rnn = nn.LSTM(self.beat_hidden_size * 2, self.measure_hidden_size, self.num_measure_layers, batch_first=True, bidirectional=True)
        # self.tempo_attention = nn.Linear(self.output_size-1, self.output_size-1)

        self.beat_tempo_forward = nn.LSTM(self.beat_hidden_size*2 + 5 + 3 + self.encoder_size, self.beat_hidden_size, num_layers=1, batch_first=True, bidirectional=False)

        self.output_lstm = nn.LSTM(self.final_input, self.final_hidden_size, num_layers=1, batch_first=True, bidirectional=False)

        if self.loss_type == 'MSE':
            self.fc = nn.Linear(self.final_hidden_size, self.output_size - 1)
            self.beat_tempo_fc = nn.Linear(self.beat_hidden_size, 1)
        elif self.loss_type == 'CE':
            self.fc = nn.Sequential(
                nn.Linear(self.final_hidden_size, self.output_size - self.tempo_output_length),
                nn.Sigmoid(),
                # nn.Softmax(dim=2)
            )
            self.beat_tempo_fc = nn.Sequential(
                nn.Linear(self.beat_hidden_size, self.tempo_output_length),
                nn.Sigmoid(),
                # nn.Softmax(dim=2)
            )
        else:
            print('Error in Constructing Network: unclassified loss type', self.loss_type)

        self.note_fc = nn.Sequential(
            nn.Linear(self.input_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
        )

        self.graph_1st = GatedGraph(self.note_hidden_size, self.num_edge_types, self.device)
        self.graph_between = nn.Sequential(
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.ReLU()
        )
        self.graph_2nd = GatedGraph(self.note_hidden_size, self.num_edge_types, self.device)

        self.performance_graph_encoder = GatedGraph(self.encoder_size, self.num_edge_types, self.device)
        self.performance_measure_attention = nn.Linear(self.encoder_size, self.encoder_size)

        self.performance_contractor = nn.Sequential(
            nn.Linear(self.encoder_input_size, self.encoder_size),
            nn.Dropout(DROP_OUT),
            # nn.BatchNorm1d(self.encoder_size),
            nn.ReLU()
        )

        self.performance_encoder = nn.LSTM(self.encoder_size, self.encoder_size,  num_layers=self.encoder_layer_num, batch_first=True, bidirectional=False)
        self.performance_encoder_mean = nn.Linear(self.encoder_size, self.encoder_size)
        self.performance_encoder_var = nn.Linear(self.encoder_size, self.encoder_size)

        self.softmax = nn.Softmax(dim=0)
        self.sigmoid = nn.Sigmoid()
        self.tanh = nn.Tanh()


    def forward(self, x, y, edges, note_locations, start_index, step_by_step = False, initial_z=False, rand_threshold=0.7):
        beat_numbers = [x.beat for x in note_locations]
        measure_numbers = [x.measure for x in note_locations]
        voice_numbers = [x.voice for x in note_locations]
        onset_numbers = [x.onset for x in note_locations]
        num_notes = x.size(1)

        note_out, beat_hidden_out, measure_hidden_out = \
            self.run_offline_score_model(x, edges, onset_numbers, beat_numbers, measure_numbers, voice_numbers, start_index)
        beat_out_spanned = self.span_beat_to_note_num(beat_hidden_out, beat_numbers, num_notes, start_index)
        measure_out_spanned = self.span_beat_to_note_num(measure_hidden_out, measure_numbers, num_notes, start_index)
        if initial_z:
            perform_z = torch.Tensor(initial_z).to(self.device).view(1,-1)
            perform_mu = 0
            perform_var = 0
        else:
            perform_concat = torch.cat((note_out, beat_out_spanned, measure_out_spanned, y), 2).view(-1, self.encoder_input_size)
            perform_style_contracted = self.performance_contractor(perform_concat).view(1, num_notes, -1)
            perform_style_graphed = self.performance_graph_encoder(perform_style_contracted, edges)
            performance_measure_nodes = self.make_higher_node(perform_style_graphed, self.performance_measure_attention, beat_numbers,
                                                  measure_numbers, start_index, lower_is_note=True)
            perform_style_encoded, _ = self.performance_encoder(performance_measure_nodes)

            # perform_style_reduced = perform_style_reduced.view(-1,self.encoder_input_size)
            # perform_style_node = self.sum_with_attention(perform_style_reduced, self.perform_attention)
            perform_style_vector = perform_style_encoded[:, -1, :]  # need check
            perform_z, perform_mu, perform_var = \
                self.encode_with_net(perform_style_vector, self.performance_encoder_mean, self.performance_encoder_var)

        # perform_z = self.performance_decoder(perform_z)
        perform_z_batched = perform_z.repeat(x.shape[1], 1).view(1,x.shape[1], -1)
        num_notes = x.size(1)
        num_beats = beat_hidden_out.size(1)

        tempo_hidden = self.init_beat_tempo_forward(x.size(0))
        final_hidden = self.init_final_layer(x.size(0))


        # Calculate tempo of the beats first
        qpm_primo = x[:,:, QPM_PRIMO_IDX].view(1, -1, 1)
        tempo_primo = x[:,:, TEMPO_PRIMO_IDX:].view(1, -1, 2)
        # beat_tempos = self.note_tempo_infos_to_beat(y, beat_numbers, start_index, QPM_INDEX)
        beat_qpm_primo = qpm_primo[0,0,0].repeat((1, num_beats, 1))
        beat_tempo_primo = tempo_primo[0,0,:].repeat((1, num_beats, 1))
        beat_tempo_vector = self.note_tempo_infos_to_beat(x, beat_numbers, start_index, TEMPO_IDX)
        # measure_out_in_beat
        if 'beat_hidden_out' not in locals():
            beat_hidden_out = beat_out_spanned
        # score_z_beat_spanned = score_z.repeat(num_beats,1).view(1,num_beats,-1)
        perform_z_beat_spanned = perform_z.repeat(num_beats,1).view(1,num_beats,-1)
        beat_tempo_cat = torch.cat((beat_hidden_out, beat_qpm_primo, beat_tempo_primo, beat_tempo_vector, perform_z_beat_spanned), 2)
        beat_forward, tempo_hidden = self.beat_tempo_forward(beat_tempo_cat, tempo_hidden)
        tempos = self.beat_tempo_fc(beat_forward)
        tempos_spanned = self.span_beat_to_note_num(tempos, beat_numbers, num_notes, start_index)
        # y[0, :, 0] = tempos_spanned.view(-1)


        # mean_velocity_info = x[:, :, mean_vel_start_index:mean_vel_start_index+4].view(1,-1,4)
        # dynamic_info = torch.cat((x[:, :, mean_vel_start_index + 4].view(1,-1,1),
        #                           x[:, :, vel_vec_start_index:vel_vec_start_index + 4]), 2).view(1,-1,5)

        out_combined = torch.cat((
            note_out, beat_out_spanned, measure_out_spanned,
            # qpm_primo, tempo_primo, mean_velocity_info, dynamic_info,
            perform_z_batched), 2)

        out, final_hidden = self.output_lstm(out_combined, final_hidden)

        out = self.fc(out)
        # out = torch.cat((out, trill_out), 2)

        out = torch.cat((tempos_spanned, out), 2)

        score_out_combined = torch.cat((note_out, beat_out_spanned, measure_out_spanned),2)
        return out, perform_mu, perform_var, score_out_combined

    def run_offline_score_model(self, x, edges, onset_numbers, beat_numbers, measure_numbers, voice_numbers, start_index):
        x = x[0,:,:]
        beat_hidden = self.init_beat_layer(1)
        measure_hidden = self.init_measure_layer(1)

        note_out = self.run_graph_network(x, edges)
        note_out = note_out.view(1,note_out.shape[0], note_out.shape[1])
        # note_out, onset_out = self.run_onset_rnn(x, voice_out, onset_numbers, start_index)
        # hidden_out, hidden = self.lstm(x, hidden)  # out: tensor of shape (batch_size, seq_length, hidden_size*2)
        # beat_nodes = self.make_higher_node(onset_out, self.beat_attention, onset_numbers, beat_numbers, start_index)
        beat_nodes = self.make_beat_node(note_out, beat_numbers, start_index)
        beat_hidden_out, beat_hidden = self.beat_rnn(beat_nodes, beat_hidden)
        measure_nodes = self.make_higher_node(beat_hidden_out, self.measure_attention, beat_numbers, measure_numbers, start_index)
        # measure_nodes = self.make_measure_node(beat_hidden_out, measure_numbers, beat_numbers, start_index)
        measure_hidden_out, measure_hidden = self.measure_rnn(measure_nodes, measure_hidden)

        return note_out, beat_hidden_out, measure_hidden_out

    def run_graph_network(self, nodes, graph_matrix):
        # 1. Run feed-forward network by note level
        notes_hidden = self.note_fc(nodes)

        notes_hidden = self.graph_1st(notes_hidden, graph_matrix)

        notes_between = self.graph_between(notes_hidden)

        notes_hidden_second = self.graph_2nd(notes_between, graph_matrix)

        notes_hidden = torch.cat((notes_hidden, notes_hidden_second),-1)

        return notes_hidden


    def encode_with_net(self, score_input, mean_net, var_net):
        mu = mean_net(score_input)
        var = var_net(score_input)

        z = self.reparameterize(mu, var)
        return z, mu, var

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5*logvar)
        eps = torch.randn_like(std)
        return eps.mul(std).add_(mu)

    # def decode_with_net(self, z, decode_network):
    #     decode_network
    #     return

    def sum_with_attention(self, hidden, attention_net):
        attention = attention_net(hidden)
        attention = self.softmax(attention)
        upper_node = hidden * attention
        upper_node_sum = torch.sum(upper_node, dim=0)

        return upper_node_sum

    def make_higher_node(self, lower_out, attention_weights, lower_indexes, higher_indexes, start_index, lower_is_note=False):
        higher_nodes = []
        prev_higher_index = higher_indexes[start_index]
        lower_node_start = 0
        lower_node_end = 0
        num_lower_nodes = lower_out.shape[1]
        start_lower_index = lower_indexes[start_index]
        lower_hidden_size = lower_out.shape[2]
        for low_index in range(num_lower_nodes):
            absolute_low_index = start_lower_index + low_index
            if lower_is_note:
                current_note_index = start_index + low_index
            else:
                current_note_index = lower_indexes.index(absolute_low_index)

            if higher_indexes[current_note_index] > prev_higher_index:
                # new beat start
                lower_node_end = low_index
                corresp_lower_out = lower_out[0, lower_node_start:lower_node_end, :]
                higher = self.sum_with_attention(corresp_lower_out, attention_weights)
                higher_nodes.append(higher)

                lower_node_start = low_index
                prev_higher_index = higher_indexes[current_note_index]

        corresp_lower_out = lower_out[0, lower_node_start:, :]
        higher = self.sum_with_attention(corresp_lower_out, attention_weights)
        higher_nodes.append(higher)

        higher_nodes = torch.stack(higher_nodes).view(1, -1, lower_hidden_size)

        return higher_nodes


    def make_beat_node(self, hidden_out, beat_number, start_index):
        beat_nodes = []
        prev_beat = beat_number[start_index]
        beat_notes_start = 0
        beat_notes_end = 0
        num_notes = hidden_out.shape[1]
        for note_index in range(num_notes):
            actual_index = start_index + note_index
            if beat_number[actual_index] > prev_beat:
                #new beat start
                beat_notes_end = note_index
                corresp_hidden = hidden_out[0, beat_notes_start:beat_notes_end, :]
                beat = self.sum_with_attention(corresp_hidden, self.beat_attention)
                beat_nodes.append(beat)

                beat_notes_start = note_index
                prev_beat = beat_number[actual_index]

        last_hidden =  hidden_out[0, beat_notes_end:, :]
        beat = self.sum_with_attention(last_hidden, self.beat_attention)
        beat_nodes.append(beat)

        beat_nodes = torch.stack(beat_nodes).view(1, -1, self.note_hidden_size * 2)
        # beat_nodes = torch.Tensor(beat_nodes)

        return beat_nodes

    def make_measure_node(self, beat_out, measure_number, beat_number, start_index):
        measure_nodes = []
        prev_measure = measure_number[start_index]
        measure_beats_start = 0
        measure_beats_end = 0
        num_beats = beat_out.shape[1]
        start_beat = beat_number[start_index]
        for beat_index in range(num_beats):
            current_beat = start_beat + beat_index
            current_note_index = beat_number.index(current_beat)

            if measure_number[current_note_index] > prev_measure:
                # new beat start
                measure_beats_end = beat_index
                corresp_hidden = beat_out[0, measure_beats_start:measure_beats_end, :]
                measure = self.sum_with_attention(corresp_hidden, self.measure_attention)
                measure_nodes.append(measure)

                measure_beats_start = beat_index
                prev_measure = measure_number[beat_index]

        last_hidden = beat_out[0, measure_beats_end:, :]
        measure = self.sum_with_attention(last_hidden, self.measure_attention)
        measure_nodes.append(measure)

        measure_nodes = torch.stack(measure_nodes).view(1,-1,self.beat_hidden_size*2)

        return measure_nodes

    def span_beat_to_note_num(self, beat_out, beat_number, num_notes, start_index):
        start_beat = beat_number[start_index]
        num_beat = beat_out.shape[1]
        span_mat = torch.zeros(1, num_notes, num_beat)
        node_size = beat_out.shape[2]
        for i in range(num_notes):
            beat_index = beat_number[start_index+i] - start_beat
            if beat_index >= num_beat:
                beat_index = num_beat-1
            span_mat[0,i,beat_index] = 1
        span_mat = span_mat.to(self.device)

        spanned_beat = torch.bmm(span_mat, beat_out)
        return spanned_beat

    def note_tempo_infos_to_beat(self, y, beat_numbers, start_index, index=None):
        beat_tempos = []
        num_notes = y.size(1)
        prev_beat = -1
        for i in range(num_notes):
            cur_beat = beat_numbers[start_index+i]
            if cur_beat > prev_beat:
                if index is None:
                    beat_tempos.append(y[0,i,:])
                if index == TEMPO_IDX:
                    beat_tempos.append(y[0,i,TEMPO_IDX:TEMPO_IDX+5])
                else:
                    beat_tempos.append(y[0,i,index])
                prev_beat = cur_beat
        num_beats = len(beat_tempos)
        beat_tempos = torch.stack(beat_tempos).view(1,num_beats,-1)
        return beat_tempos

    def measure_to_beat_span(self, measure_out, measure_numbers, beat_numbers, start_index):
        pass


    def run_voice_net(self, batch_x, voice_hidden, voice_numbers, max_voice):
        num_notes = batch_x.size(1)
        output = torch.zeros(1, batch_x.size(1), self.voice_hidden_size * 2).to(self.device)
        voice_numbers = torch.Tensor(voice_numbers)
        for i in range(1,max_voice+1):
            voice_x_bool = voice_numbers == i
            num_voice_notes = torch.sum(voice_x_bool)
            if num_voice_notes > 0:
                span_mat = torch.zeros(num_notes, num_voice_notes)
                note_index_in_voice = 0
                for j in range(num_notes):
                    if voice_x_bool[j] ==1:
                        span_mat[j, note_index_in_voice] = 1
                        note_index_in_voice += 1
                span_mat = span_mat.view(1,num_notes,-1).to(self.device)
                voice_x = batch_x[0,voice_x_bool,:].view(1,-1, self.input_size)
                ith_hidden = voice_hidden[i-1]

                ith_voice_out, ith_hidden = self.voice_net(voice_x, ith_hidden)
                # ith_voice_out, ith_hidden = self.lstm(voice_x, ith_hidden)
                output += torch.bmm(span_mat, ith_voice_out)
        return output, voice_hidden

    def init_hidden(self, batch_size):
        h0 = torch.zeros(self.num_layers * 2, batch_size, self.note_hidden_size).to(self.device)
        return (h0, h0)

    def init_final_layer(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.final_hidden_size).to(self.device)
        return (h0, h0)

    def init_onset_layer(self, batch_size):
        h0 = torch.zeros(self.num_onset_layers * 2, batch_size, self.onset_hidden_size).to(self.device)
        return (h0, h0)

    def init_beat_layer(self, batch_size):
        h0 = torch.zeros(self.num_beat_layers * 2, batch_size, self.beat_hidden_size).to(self.device)
        return (h0, h0)

    def init_measure_layer(self, batch_size):
        h0 = torch.zeros(self.num_measure_layers * 2, batch_size, self.measure_hidden_size).to(self.device)
        return (h0, h0)

    def init_beat_tempo_forward(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.beat_hidden_size).to(self.device)
        return (h0, h0)

    def init_voice_layer(self, batch_size, max_voice):
        layers = []
        for i in range(max_voice):
            # h0 = torch.zeros(self.num_voice_layers * 2, batch_size, self.voice_hidden_size).to(device)
            h0 = torch.zeros(self.num_voice_layers * 2, batch_size, self.note_hidden_size).to(self.device)
            layers.append((h0, h0))
        return layers

    def init_onset_encoder(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.onset_hidden_size).to(self.device)
        return (h0, h0)


class GGNN_Recursive(nn.Module):
    def __init__(self, network_parameters, device):
        super(GGNN_Recursive, self).__init__()
        self.device = device
        self.input_size = network_parameters.input_size
        self.output_size = network_parameters.output_size
        self.num_layers = network_parameters.note.layer
        self.note_hidden_size = network_parameters.note.size
        self.num_beat_layers = network_parameters.beat.layer
        self.beat_hidden_size = network_parameters.beat.size
        self.num_measure_layers = network_parameters.measure.layer
        self.measure_hidden_size = network_parameters.measure.size
        self.final_hidden_size = network_parameters.final.size
        self.final_input = network_parameters.final.input
        self.encoder_size = network_parameters.encoder.size
        self.encoder_input_size = network_parameters.encoder.input
        self.encoder_layer_num = network_parameters.encoder.layer
        self.onset_hidden_size = network_parameters.onset.size
        self.num_onset_layers = network_parameters.onset.layer
        self.time_regressive_size = network_parameters.time_reg.size
        self.time_regressive_layer = network_parameters.time_reg.layer
        self.graph_iteration = network_parameters.graph_iteration
        self.num_edge_types = network_parameters.num_edge_types

        self.beat_attention = nn.Linear(self.note_hidden_size * 2, self.note_hidden_size * 2)
        self.beat_rnn = nn.LSTM(self.note_hidden_size * 2, self.beat_hidden_size, self.num_beat_layers, batch_first=True, bidirectional=True, dropout=DROP_OUT)
        self.measure_attention = nn.Linear(self.beat_hidden_size*2, self.beat_hidden_size*2)
        self.measure_rnn = nn.LSTM(self.beat_hidden_size * 2, self.measure_hidden_size, self.num_measure_layers, batch_first=True, bidirectional=True)
        # self.tempo_attention = nn.Linear(self.output_size-1, self.output_size-1)

        self.final_beat_attention = nn.Linear(self.final_input+self.encoder_size+self.output_size, self.final_input+self.encoder_size+self.output_size)
        self.tempo_fc = nn.Linear(self.time_regressive_size * 2, 1)
        # self.fc = nn.Linear(self.final_input + self.encoder_size + self.output_size, self.output_size - 1)
        self.fc = nn.Sequential(
            nn.Linear(self.final_input + self.encoder_size + self.output_size + self.time_regressive_size * 2 + 1, self.encoder_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),

            nn.Linear(self.encoder_size, self.output_size - 1),
        )

        self.note_fc = nn.Sequential(
            nn.Linear(self.input_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
        )

        self.graph_1st = GatedGraph(self.note_hidden_size, self.num_edge_types, self.device)
        self.graph_between = nn.Sequential(
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.ReLU()
        )
        self.graph_2nd = GatedGraph(self.note_hidden_size, self.num_edge_types, self.device)

        self.performance_graph_encoder = GatedGraph(self.encoder_size, self.num_edge_types, self.device)
        self.performance_measure_attention = nn.Linear(self.encoder_size, self.encoder_size)

        self.performance_contractor = nn.Sequential(
            nn.Linear(self.encoder_input_size, self.encoder_size),
            nn.Dropout(DROP_OUT),
            # nn.BatchNorm1d(self.encoder_size),
            nn.ReLU()
        )

        # self.final_contractor = nn.Sequential(
        #     nn.Linear(self.final_input, self.final_hidden_size),
        #     nn.Dropout(DROP_OUT),
        #     # nn.BatchNorm1d(self.encoder_size),
        #     nn.ReLU()
        # )

        self.beat_tempo_contractor = nn.Sequential(
            nn.Linear(self.final_input + self.encoder_size + self.output_size, self.time_regressive_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU()
        )

        # self.perform_style_to_measure = nn.LSTM(self.measure_hidden_size * 2 + self.encoder_size, self.encoder_size, num_layers=1, bidirectional=False)

        self.initial_result_fc = nn.Linear(self.final_input, self.output_size)
        self.final_graph = GatedGraph(self.final_input + self.encoder_size + self.output_size, self.num_edge_types, self.device, self.output_size)
        self.tempo_rnn = nn.LSTM(self.time_regressive_size + 3 + 5, self.time_regressive_size, num_layers=self.time_regressive_layer, batch_first=True, bidirectional=True)

        self.performance_encoder = nn.LSTM(self.encoder_size, self.encoder_size,  num_layers=self.encoder_layer_num, batch_first=True, bidirectional=False)
        self.performance_encoder_mean = nn.Linear(self.encoder_size, self.encoder_size)
        self.performance_encoder_var = nn.Linear(self.encoder_size, self.encoder_size)

        self.softmax = nn.Softmax(dim=0)
        self.sigmoid = nn.Sigmoid()
        self.tanh = nn.Tanh()

    def forward(self, x, y, edges, note_locations, start_index, step_by_step = False, initial_z=False, rand_threshold=0.7):
        beat_numbers = [x.beat for x in note_locations]
        measure_numbers = [x.measure for x in note_locations]
        voice_numbers = [x.voice for x in note_locations]
        onset_numbers = [x.onset for x in note_locations]
        num_notes = x.size(1)

        note_out, beat_hidden_out, measure_hidden_out = \
            self.run_offline_score_model(x, edges, onset_numbers, beat_numbers, measure_numbers, voice_numbers, start_index)
        beat_out_spanned = self.span_beat_to_note_num(beat_hidden_out, beat_numbers, num_notes, start_index)
        measure_out_spanned = self.span_beat_to_note_num(measure_hidden_out, measure_numbers, num_notes, start_index)
        if initial_z:
            perform_z = torch.Tensor(initial_z).to(self.device).view(1,-1)
            perform_mu = 0
            perform_var = 0
        else:
            encoder_hidden = self.init_performance_encoder(x.shape[0])
            perform_concat = torch.cat((note_out, beat_out_spanned, measure_out_spanned, y), 2).view(-1, self.encoder_input_size)
            perform_style_contracted = self.performance_contractor(perform_concat).view(1, num_notes, -1)
            perform_style_graphed = self.performance_graph_encoder(perform_style_contracted, edges)
            performance_measure_nodes = self.make_higher_node(perform_style_graphed, self.performance_measure_attention, beat_numbers,
                                                  measure_numbers, start_index, lower_is_note=True)
            perform_style_encoded, _ = self.performance_encoder(performance_measure_nodes, encoder_hidden)

            # perform_style_reduced = perform_style_reduced.view(-1,self.encoder_input_size)
            # perform_style_node = self.sum_with_attention(perform_style_reduced, self.perform_attention)
            perform_style_vector = perform_style_encoded[:, -1, :]  # need check
            perform_z, perform_mu, perform_var = \
                self.encode_with_net(perform_style_vector, self.performance_encoder_mean, self.performance_encoder_var)

        # perform_z = self.performance_decoder(perform_z)
        perform_z_batched = perform_z.repeat(num_notes, 1).view(1,num_notes, -1)


        # mean_velocity_info = x[:, :, mean_vel_start_index:mean_vel_start_index+4].view(1,-1,4)
        # dynamic_info = torch.cat((x[:, :, mean_vel_start_index + 4].view(1,-1,1),
        #                           x[:, :, vel_vec_start_index:vel_vec_start_index + 4]), 2).view(1,-1,5)

        out_combined = torch.cat((
            note_out, beat_out_spanned, measure_out_spanned), 2)

        # out = self.final_contractor(out_combined)
        initial_output = self.initial_result_fc(out_combined)
        # initial_output = torch.zeros(1, num_notes, self.output_size).to(self.device)
        # style_to_measure_hidden = self.init_performance_encoder(x.shape[0])
        # perform_z_measure_spanned = perform_z.repeat(measure_hidden_out.shape[1], 1).view(1,measure_hidden_out.shape[1], -1)
        # perform_z_measure_cat = torch.cat((perform_z_measure_spanned, measure_hidden_out), 2)
        # measure_perform_style, _ = self.perform_style_to_measure(perform_z_measure_cat, style_to_measure_hidden)
        # measure_perform_style_spanned = self.span_beat_to_note_num(measure_perform_style, measure_numbers, num_notes, start_index)
        out_with_result = torch.cat((out_combined, perform_z_batched, initial_output), 2)
        tempo_hidden = self.init_beat_tempo_forward(x.shape[0])

        num_beats = beat_hidden_out.shape[1]
        qpm_primo = x[:, :, QPM_PRIMO_IDX].view(1, -1, 1)
        tempo_primo = x[:, :, TEMPO_PRIMO_IDX:].view(1, -1, 2)
        # beat_tempos = self.note_tempo_infos_to_beat(y, beat_numbers, start_index, QPM_INDEX)
        beat_qpm_primo = qpm_primo[0, 0, 0].repeat((1, num_beats, 1))
        beat_tempo_primo = tempo_primo[0, 0, :].repeat((1, num_beats, 1))
        beat_tempo_vector = self.note_tempo_infos_to_beat(x, beat_numbers, start_index, TEMPO_IDX)

        for i in range(5):
            out_with_result = self.final_graph(out_with_result, edges, iteration=20)
            out_beat = self.make_higher_node(out_with_result, self.final_beat_attention, beat_numbers,
                                             beat_numbers, start_index, lower_is_note=True)
            out_beat = self.beat_tempo_contractor(out_beat)
            tempo_beat_cat = torch.cat((out_beat, beat_qpm_primo, beat_tempo_primo, beat_tempo_vector ),2)
            out_beat_rnn_result, _ = self.tempo_rnn(tempo_beat_cat, tempo_hidden)
            tempo_out = self.tempo_fc(out_beat_rnn_result)
            tempos_spanned = self.span_beat_to_note_num(tempo_out, beat_numbers, num_notes, start_index)
            out_beat_spanned = self.span_beat_to_note_num(out_beat_rnn_result, beat_numbers, num_notes, start_index)
            out_with_beat_out = torch.cat((out_with_result, out_beat_spanned, tempos_spanned),2)
            other_out = self.fc(out_with_beat_out)

            final_out = torch.cat((tempos_spanned, other_out),2)
            out_with_result = torch.cat((out_with_result[:,:,:-self.output_size], final_out),2)
            # out = torch.cat((out, trill_out), 2)

        return final_out, perform_mu, perform_var, out_combined


    def run_offline_score_model(self, x, edges, onset_numbers, beat_numbers, measure_numbers, voice_numbers, start_index):
        x = x[0,:,:]
        beat_hidden = self.init_beat_layer(1)
        measure_hidden = self.init_measure_layer(1)

        note_out = self.run_graph_network(x, edges)
        note_out = note_out.view(1,note_out.shape[0], note_out.shape[1])
        # note_out, onset_out = self.run_onset_rnn(x, voice_out, onset_numbers, start_index)
        # hidden_out, hidden = self.lstm(x, hidden)  # out: tensor of shape (batch_size, seq_length, hidden_size*2)
        # beat_nodes = self.make_higher_node(onset_out, self.beat_attention, onset_numbers, beat_numbers, start_index)
        beat_nodes = self.make_beat_node(note_out, beat_numbers, start_index)
        beat_hidden_out, beat_hidden = self.beat_rnn(beat_nodes, beat_hidden)
        measure_nodes = self.make_higher_node(beat_hidden_out, self.measure_attention, beat_numbers, measure_numbers, start_index)
        # measure_nodes = self.make_measure_node(beat_hidden_out, measure_numbers, beat_numbers, start_index)
        measure_hidden_out, measure_hidden = self.measure_rnn(measure_nodes, measure_hidden)

        return note_out, beat_hidden_out, measure_hidden_out

    def run_graph_network(self, nodes, graph_matrix):
        # 1. Run feed-forward network by note level

        notes_hidden = self.note_fc(nodes)
        num_notes = notes_hidden.size(1)

        notes_hidden = self.graph_1st(notes_hidden, graph_matrix)
        time3 = time.time()

        notes_between = self.graph_between(notes_hidden)

        notes_hidden_second = self.graph_2nd(notes_between, graph_matrix)

        notes_hidden = torch.cat((notes_hidden, notes_hidden_second),-1)

        return notes_hidden


    def encode_with_net(self, score_input, mean_net, var_net):
        mu = mean_net(score_input)
        var = var_net(score_input)

        z = self.reparameterize(mu, var)
        return z, mu, var

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5*logvar)
        eps = torch.randn_like(std)
        return eps.mul(std).add_(mu)

    # def decode_with_net(self, z, decode_network):
    #     decode_network
    #     return

    def sum_with_attention(self, hidden, attention_net):
        attention = attention_net(hidden)
        attention = self.softmax(attention)
        upper_node = hidden * attention
        upper_node_sum = torch.sum(upper_node, dim=0)

        return upper_node_sum

    def make_higher_node(self, lower_out, attention_weights, lower_indexes, higher_indexes, start_index, lower_is_note=False):
        higher_nodes = []
        prev_higher_index = higher_indexes[start_index]
        lower_node_start = 0
        lower_node_end = 0
        num_lower_nodes = lower_out.shape[1]
        start_lower_index = lower_indexes[start_index]
        lower_hidden_size = lower_out.shape[2]
        for low_index in range(num_lower_nodes):
            absolute_low_index = start_lower_index + low_index
            if lower_is_note:
                current_note_index = start_index + low_index
            else:
                current_note_index = lower_indexes.index(absolute_low_index)

            if higher_indexes[current_note_index] > prev_higher_index:
                # new beat start
                lower_node_end = low_index
                corresp_lower_out = lower_out[0, lower_node_start:lower_node_end, :]
                higher = self.sum_with_attention(corresp_lower_out, attention_weights)
                higher_nodes.append(higher)

                lower_node_start = low_index
                prev_higher_index = higher_indexes[current_note_index]

        corresp_lower_out = lower_out[0, lower_node_start:, :]
        higher = self.sum_with_attention(corresp_lower_out, attention_weights)
        higher_nodes.append(higher)

        higher_nodes = torch.stack(higher_nodes).view(1, -1, lower_hidden_size)

        return higher_nodes


    def make_beat_node(self, hidden_out, beat_number, start_index):
        beat_nodes = []
        prev_beat = beat_number[start_index]
        beat_notes_start = 0
        beat_notes_end = 0
        num_notes = hidden_out.shape[1]
        for note_index in range(num_notes):
            actual_index = start_index + note_index
            if beat_number[actual_index] > prev_beat:
                #new beat start
                beat_notes_end = note_index
                corresp_hidden = hidden_out[0, beat_notes_start:beat_notes_end, :]
                beat = self.sum_with_attention(corresp_hidden, self.beat_attention)
                beat_nodes.append(beat)

                beat_notes_start = note_index
                prev_beat = beat_number[actual_index]

        last_hidden =  hidden_out[0, beat_notes_end:, :]
        beat = self.sum_with_attention(last_hidden, self.beat_attention)
        beat_nodes.append(beat)

        beat_nodes = torch.stack(beat_nodes).view(1, -1, self.note_hidden_size * 2)
        # beat_nodes = torch.Tensor(beat_nodes)

        return beat_nodes

    def make_measure_node(self, beat_out, measure_number, beat_number, start_index):
        measure_nodes = []
        prev_measure = measure_number[start_index]
        measure_beats_start = 0
        measure_beats_end = 0
        num_beats = beat_out.shape[1]
        start_beat = beat_number[start_index]
        for beat_index in range(num_beats):
            current_beat = start_beat + beat_index
            current_note_index = beat_number.index(current_beat)

            if measure_number[current_note_index] > prev_measure:
                # new beat start
                measure_beats_end = beat_index
                corresp_hidden = beat_out[0, measure_beats_start:measure_beats_end, :]
                measure = self.sum_with_attention(corresp_hidden, self.measure_attention)
                measure_nodes.append(measure)

                measure_beats_start = beat_index
                prev_measure = measure_number[beat_index]

        last_hidden = beat_out[0, measure_beats_end:, :]
        measure = self.sum_with_attention(last_hidden, self.measure_attention)
        measure_nodes.append(measure)

        measure_nodes = torch.stack(measure_nodes).view(1,-1,self.beat_hidden_size*2)

        return measure_nodes

    def span_beat_to_note_num(self, beat_out, beat_number, num_notes, start_index):
        start_beat = beat_number[start_index]
        num_beat = beat_out.shape[1]
        span_mat = torch.zeros(1, num_notes, num_beat)
        node_size = beat_out.shape[2]
        for i in range(num_notes):
            beat_index = beat_number[start_index+i] - start_beat
            if beat_index >= num_beat:
                beat_index = num_beat-1
            span_mat[0,i,beat_index] = 1
        span_mat = span_mat.to(self.device)

        spanned_beat = torch.bmm(span_mat, beat_out)
        return spanned_beat

    def note_tempo_infos_to_beat(self, y, beat_numbers, start_index, index=None):
        beat_tempos = []
        num_notes = y.size(1)
        prev_beat = -1
        for i in range(num_notes):
            cur_beat = beat_numbers[start_index+i]
            if cur_beat > prev_beat:
                if index is None:
                    beat_tempos.append(y[0,i,:])
                if index == TEMPO_IDX:
                    beat_tempos.append(y[0,i,TEMPO_IDX:TEMPO_IDX+5])
                else:
                    beat_tempos.append(y[0,i,index])
                prev_beat = cur_beat
        num_beats = len(beat_tempos)
        beat_tempos = torch.stack(beat_tempos).view(1,num_beats,-1)
        return beat_tempos

    def measure_to_beat_span(self, measure_out, measure_numbers, beat_numbers, start_index):
        pass


    def run_voice_net(self, batch_x, voice_hidden, voice_numbers, max_voice):
        num_notes = batch_x.size(1)
        output = torch.zeros(1, batch_x.size(1), self.voice_hidden_size * 2).to(self.device)
        voice_numbers = torch.Tensor(voice_numbers)
        for i in range(1,max_voice+1):
            voice_x_bool = voice_numbers == i
            num_voice_notes = torch.sum(voice_x_bool)
            if num_voice_notes > 0:
                span_mat = torch.zeros(num_notes, num_voice_notes)
                note_index_in_voice = 0
                for j in range(num_notes):
                    if voice_x_bool[j] ==1:
                        span_mat[j, note_index_in_voice] = 1
                        note_index_in_voice += 1
                span_mat = span_mat.view(1,num_notes,-1).to(self.device)
                voice_x = batch_x[0,voice_x_bool,:].view(1,-1, self.input_size)
                ith_hidden = voice_hidden[i-1]

                ith_voice_out, ith_hidden = self.voice_net(voice_x, ith_hidden)
                # ith_voice_out, ith_hidden = self.lstm(voice_x, ith_hidden)
                output += torch.bmm(span_mat, ith_voice_out)
        return output, voice_hidden

    def init_hidden(self, batch_size):
        h0 = torch.zeros(self.num_layers * 2, batch_size, self.note_hidden_size).to(self.device)
        return (h0, h0)

    def init_final_layer(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.final_hidden_size).to(self.device)
        return (h0, h0)

    def init_onset_layer(self, batch_size):
        h0 = torch.zeros(self.num_onset_layers * 2, batch_size, self.onset_hidden_size).to(self.device)
        return (h0, h0)

    def init_beat_layer(self, batch_size):
        h0 = torch.zeros(self.num_beat_layers * 2, batch_size, self.beat_hidden_size).to(self.device)
        return (h0, h0)

    def init_measure_layer(self, batch_size):
        h0 = torch.zeros(self.num_measure_layers * 2, batch_size, self.measure_hidden_size).to(self.device)
        return (h0, h0)

    def init_beat_tempo_forward(self, batch_size):
        h0 = torch.zeros(1 * 2, batch_size, self.time_regressive_size).to(self.device)
        return (h0, h0)

    def init_performance_encoder(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.encoder_size).to(self.device)


class Sequential_GGNN(nn.Module):
    def __init__(self, network_parameters, device):
        super(Sequential_GGNN, self).__init__()
        self.device = device
        self.input_size = network_parameters.input_size
        self.output_size = network_parameters.output_size
        self.num_layers = network_parameters.note.layer
        self.note_hidden_size = network_parameters.note.size
        self.num_measure_layers = network_parameters.measure.layer
        self.measure_hidden_size = network_parameters.measure.size
        self.final_hidden_size = network_parameters.final.size
        self.final_input = network_parameters.final.input
        self.encoder_size = network_parameters.encoder.size
        self.encoder_input_size = network_parameters.encoder.input
        self.encoder_layer_num = network_parameters.encoder.layer
        self.onset_hidden_size = network_parameters.onset.size
        self.num_onset_layers = network_parameters.onset.layer
        self.time_regressive_size = network_parameters.time_reg.size
        self.time_regressive_layer = network_parameters.time_reg.layer
        self.graph_iteration = network_parameters.graph_iteration
        self.num_edge_types = network_parameters.num_edge_types

        self.note_fc = nn.Sequential(
            nn.Linear(self.input_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
        )

        self.graph_1st = GatedGraph(self.note_hidden_size + self.measure_hidden_size * 2, self.num_edge_types, self.device, secondary_size=self.note_hidden_size)
        self.graph_between = nn.Sequential(
            nn.Linear(self.note_hidden_size + self.measure_hidden_size * 2, self.note_hidden_size + self.measure_hidden_size * 2),
            nn.Dropout(DROP_OUT),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.ReLU()
        )
        self.graph_2nd = GatedGraph(self.note_hidden_size + self.measure_hidden_size * 2, self.num_edge_types, self.device)

        self.measure_attention = nn.Linear(self.note_hidden_size * 2, self.note_hidden_size * 2)
        self.measure_rnn = nn.LSTM(self.note_hidden_size * 2, self.measure_hidden_size, self.num_measure_layers, batch_first=True, bidirectional=True)
        # self.tempo_attention = nn.Linear(self.output_size-1, self.output_size-1)

        self.performance_graph_encoder = GatedGraph(self.encoder_size, self.num_edge_types, self.device)
        self.performance_measure_attention = nn.Linear(self.encoder_size, self.encoder_size)

        self.performance_contractor = nn.Sequential(
            nn.Linear(self.encoder_input_size, self.encoder_size),
            nn.Dropout(DROP_OUT),
            # nn.BatchNorm1d(self.encoder_size),
            nn.ReLU()
        )
        self.performance_encoder = nn.LSTM(self.encoder_size, self.encoder_size, num_layers=self.encoder_layer_num,
                                           batch_first=True, bidirectional=False)
        self.performance_encoder_mean = nn.Linear(self.encoder_size, self.encoder_size)
        self.performance_encoder_var = nn.Linear(self.encoder_size, self.encoder_size)

        self.beat_tempo_contractor = nn.Sequential(
            nn.Linear(self.final_input + self.encoder_size + self.output_size, self.time_regressive_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU()
        )

        # self.perform_style_to_measure = nn.LSTM(self.measure_hidden_size * 2 + self.encoder_size, self.encoder_size, num_layers=1, bidirectional=False)

        self.initial_result_fc = nn.Linear(self.final_input, self.output_size)
        self.final_graph = GatedGraph(self.final_input + self.encoder_size + self.output_size, self.num_edge_types, self.device, self.output_size)
        self.tempo_rnn = nn.LSTM(self.time_regressive_size + 3 + 5, self.time_regressive_size, num_layers=self.time_regressive_layer, batch_first=True, bidirectional=True)

        self.final_beat_attention = nn.Linear(self.final_input+self.encoder_size+self.output_size, self.final_input+self.encoder_size+self.output_size)
        self.tempo_fc = nn.Linear(self.time_regressive_size * 2, 1)
        # self.fc = nn.Linear(self.final_input + self.encoder_size + self.output_size, self.output_size - 1)
        self.fc = nn.Sequential(
            nn.Linear(self.final_input + self.encoder_size + self.output_size + self.time_regressive_size * 2 + 1, self.encoder_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),

            nn.Linear(self.encoder_size, self.output_size - 1),
        )

        self.softmax = nn.Softmax(dim=0)
        self.sigmoid = nn.Sigmoid()
        self.tanh = nn.Tanh()

    def forward(self, x, y, edges, note_locations, start_index, initial_z=False, return_z=False):
        beat_numbers = [x.beat for x in note_locations]
        measure_numbers = [x.measure for x in note_locations]
        num_notes = x.size(1)

        note_out = self.run_graph_network(x, edges, measure_numbers, start_index)
        if type(initial_z) is not bool:
            if type(initial_z) is list or not initial_z.is_cuda:
                perform_z = torch.Tensor(initial_z).to(self.device).view(1,-1)
            else:
                perform_z = initial_z.view(1,-1)
            perform_mu = 0
            perform_var = 0
        else:
            encoder_hidden = self.init_performance_encoder(x.shape[0])
            perform_concat = torch.cat((note_out, y), 2).view(-1, self.encoder_input_size)
            perform_style_contracted = self.performance_contractor(perform_concat).view(1, num_notes, -1)
            perform_style_graphed = self.performance_graph_encoder(perform_style_contracted, edges)
            performance_measure_nodes = self.make_higher_node(perform_style_graphed, self.performance_measure_attention, beat_numbers,
                                                  measure_numbers, start_index, lower_is_note=True)
            perform_style_encoded, _ = self.performance_encoder(performance_measure_nodes, encoder_hidden)

            # perform_style_reduced = perform_style_reduced.view(-1,self.encoder_input_size)
            # perform_style_node = self.sum_with_attention(perform_style_reduced, self.perform_attention)
            perform_style_vector = perform_style_encoded[:, -1, :]  # need check
            perform_z, perform_mu, perform_var = \
                self.encode_with_net(perform_style_vector, self.performance_encoder_mean, self.performance_encoder_var)
        if return_z:
            return perform_z
        # perform_z = self.performance_decoder(perform_z)
        perform_z_batched = perform_z.repeat(num_notes, 1).view(1,num_notes, -1)

        # mean_velocity_info = x[:, :, mean_vel_start_index:mean_vel_start_index+4].view(1,-1,4)
        # dynamic_info = torch.cat((x[:, :, mean_vel_start_index + 4].view(1,-1,1),
        #                           x[:, :, vel_vec_start_index:vel_vec_start_index + 4]), 2).view(1,-1,5)

        # out_combined = torch.cat((
        #     note_out, measure_out_spanned), 2)

        # out = self.final_contractor(out_combined)
        initial_output = self.initial_result_fc(note_out)
        # initial_output = torch.zeros(1, num_notes, self.output_size).to(self.device)
        # style_to_measure_hidden = self.init_performance_encoder(x.shape[0])
        # perform_z_measure_spanned = perform_z.repeat(measure_hidden_out.shape[1], 1).view(1,measure_hidden_out.shape[1], -1)
        # perform_z_measure_cat = torch.cat((perform_z_measure_spanned, measure_hidden_out), 2)
        # measure_perform_style, _ = self.perform_style_to_measure(perform_z_measure_cat, style_to_measure_hidden)
        # measure_perform_style_spanned = self.span_beat_to_note_num(measure_perform_style, measure_numbers, num_notes, start_index)
        out_with_result = torch.cat((note_out, perform_z_batched, initial_output), 2)
        tempo_hidden = self.init_beat_tempo_forward(x.shape[0])

        num_beats = beat_numbers[start_index+num_notes-1] - beat_numbers[start_index] + 1
        qpm_primo = x[:, :, QPM_PRIMO_IDX].view(1, -1, 1)
        tempo_primo = x[:, :, TEMPO_PRIMO_IDX:].view(1, -1, 2)
        # beat_tempos = self.note_tempo_infos_to_beat(y, beat_numbers, start_index, QPM_INDEX)
        beat_qpm_primo = qpm_primo[0, 0, 0].repeat((1, num_beats, 1))
        beat_tempo_primo = tempo_primo[0, 0, :].repeat((1, num_beats, 1))
        beat_tempo_vector = self.note_tempo_infos_to_beat(x, beat_numbers, start_index, TEMPO_IDX)

        for i in range(5):
            out_with_result = self.final_graph(out_with_result, edges, iteration=self.graph_iteration)
            out_beat = self.make_higher_node(out_with_result, self.final_beat_attention, beat_numbers,
                                             beat_numbers, start_index, lower_is_note=True)
            out_beat = self.beat_tempo_contractor(out_beat)
            tempo_beat_cat = torch.cat((out_beat, beat_qpm_primo, beat_tempo_primo, beat_tempo_vector ),2)
            out_beat_rnn_result, _ = self.tempo_rnn(tempo_beat_cat, tempo_hidden)
            tempo_out = self.tempo_fc(out_beat_rnn_result)
            tempos_spanned = self.span_beat_to_note_num(tempo_out, beat_numbers, num_notes, start_index)
            out_beat_spanned = self.span_beat_to_note_num(out_beat_rnn_result, beat_numbers, num_notes, start_index)
            out_with_beat_out = torch.cat((out_with_result, out_beat_spanned, tempos_spanned),2)
            other_out = self.fc(out_with_beat_out)

            final_out = torch.cat((tempos_spanned, other_out),2)
            out_with_result = torch.cat((out_with_result[:,:,:-self.output_size], final_out),2)
            # out = torch.cat((out, trill_out), 2)

        return final_out, perform_mu, perform_var, note_out

    def run_graph_network(self, nodes, adjacency_matrix, measure_numbers, start_index, iteration=5):
        # 1. Run feed-forward network by note level
        num_notes = nodes.shape[1]
        notes_hidden = self.note_fc(nodes)
        initial_measure = torch.zeros((notes_hidden.size(0), notes_hidden.size(1), self.measure_hidden_size * 2)).to(self.device)
        notes_and_measure_hidden = torch.cat((initial_measure, notes_hidden), 2)
        for i in range(iteration):
            notes_hidden = self.graph_1st(notes_and_measure_hidden, adjacency_matrix, iteration=self.graph_iteration)
            notes_between = self.graph_between(notes_hidden)
            notes_hidden_second = self.graph_2nd(notes_between, adjacency_matrix, iteration=self.graph_iteration)
            notes_hidden_cat = torch.cat((notes_hidden[:,:, -self.note_hidden_size:],
                                          notes_hidden_second[:,:, -self.note_hidden_size:]), -1)

            measure_nodes = self.make_higher_node(notes_hidden_cat, self.measure_attention, measure_numbers, measure_numbers,
                                                  start_index, lower_is_note=True)
            initial_measure_hidden = self.init_measure_layer(1)
            measure_hidden, _ = self.measure_rnn(measure_nodes, initial_measure_hidden)
            measure_hidden_spanned = self.span_beat_to_note_num(measure_hidden, measure_numbers, num_notes, start_index)
            notes_hidden = torch.cat((measure_hidden_spanned, notes_hidden[:,:,-self.note_hidden_size:]),-1)

        final_out = torch.cat((notes_hidden, notes_hidden_second),-1)
        return final_out

    def encode_with_net(self, score_input, mean_net, var_net):
        mu = mean_net(score_input)
        var = var_net(score_input)

        z = self.reparameterize(mu, var)
        return z, mu, var

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5*logvar)
        eps = torch.randn_like(std)
        return eps.mul(std).add_(mu)

    # def decode_with_net(self, z, decode_network):
    #     decode_network
    #     return

    def sum_with_attention(self, hidden, attention_net):
        attention = attention_net(hidden)
        attention = self.softmax(attention)
        upper_node = hidden * attention
        upper_node_sum = torch.sum(upper_node, dim=0)

        return upper_node_sum

    def make_higher_node(self, lower_out, attention_weights, lower_indexes, higher_indexes, start_index, lower_is_note=False):
        higher_nodes = []
        prev_higher_index = higher_indexes[start_index]
        lower_node_start = 0
        lower_node_end = 0
        num_lower_nodes = lower_out.shape[1]
        start_lower_index = lower_indexes[start_index]
        lower_hidden_size = lower_out.shape[2]
        for low_index in range(num_lower_nodes):
            absolute_low_index = start_lower_index + low_index
            if lower_is_note:
                current_note_index = start_index + low_index
            else:
                current_note_index = lower_indexes.index(absolute_low_index)

            if higher_indexes[current_note_index] > prev_higher_index:
                # new beat start
                lower_node_end = low_index
                corresp_lower_out = lower_out[0, lower_node_start:lower_node_end, :]
                higher = self.sum_with_attention(corresp_lower_out, attention_weights)
                higher_nodes.append(higher)

                lower_node_start = low_index
                prev_higher_index = higher_indexes[current_note_index]

        corresp_lower_out = lower_out[0, lower_node_start:, :]
        higher = self.sum_with_attention(corresp_lower_out, attention_weights)
        higher_nodes.append(higher)

        higher_nodes = torch.stack(higher_nodes).view(1, -1, lower_hidden_size)

        return higher_nodes


    def span_beat_to_note_num(self, beat_out, beat_number, num_notes, start_index):
        start_beat = beat_number[start_index]
        num_beat = beat_out.shape[1]
        span_mat = torch.zeros(1, num_notes, num_beat)
        node_size = beat_out.shape[2]
        for i in range(num_notes):
            beat_index = beat_number[start_index+i] - start_beat
            if beat_index >= num_beat:
                beat_index = num_beat-1
            span_mat[0,i,beat_index] = 1
        span_mat = span_mat.to(self.device)

        spanned_beat = torch.bmm(span_mat, beat_out)
        return spanned_beat

    def note_tempo_infos_to_beat(self, y, beat_numbers, start_index, index=None):
        beat_tempos = []
        num_notes = y.size(1)
        prev_beat = -1
        for i in range(num_notes):
            cur_beat = beat_numbers[start_index+i]
            if cur_beat > prev_beat:
                if index is None:
                    beat_tempos.append(y[0,i,:])
                if index == TEMPO_IDX:
                    beat_tempos.append(y[0,i,TEMPO_IDX:TEMPO_IDX+5])
                else:
                    beat_tempos.append(y[0,i,index])
                prev_beat = cur_beat
        num_beats = len(beat_tempos)
        beat_tempos = torch.stack(beat_tempos).view(1,num_beats,-1)
        return beat_tempos

    def init_final_layer(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.final_hidden_size).to(self.device)
        return (h0, h0)

    def init_measure_layer(self, batch_size):
        h0 = torch.zeros(self.num_measure_layers * 2, batch_size, self.measure_hidden_size).to(self.device)
        return (h0, h0)

    def init_beat_tempo_forward(self, batch_size):
        h0 = torch.zeros(1 * 2, batch_size, self.time_regressive_size).to(self.device)
        return (h0, h0)

    def init_performance_encoder(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.encoder_size).to(self.device)

class SGGNN_Alt(nn.Module):
    def __init__(self, network_parameters, device):
        super(SGGNN_Alt, self).__init__()
        self.device = device
        self.input_size = network_parameters.input_size
        self.output_size = network_parameters.output_size
        self.num_layers = network_parameters.note.layer
        self.note_hidden_size = network_parameters.note.size
        self.num_measure_layers = network_parameters.measure.layer
        self.measure_hidden_size = network_parameters.measure.size
        self.final_hidden_size = network_parameters.final.size
        self.final_input = network_parameters.final.input
        self.encoder_size = network_parameters.encoder.size
        self.encoder_input_size = network_parameters.encoder.input
        self.encoder_layer_num = network_parameters.encoder.layer
        self.onset_hidden_size = network_parameters.onset.size
        self.num_onset_layers = network_parameters.onset.layer
        self.time_regressive_size = network_parameters.time_reg.size
        self.time_regressive_layer = network_parameters.time_reg.layer
        self.graph_iteration = network_parameters.graph_iteration
        self.num_edge_types = network_parameters.num_edge_types

        self.note_fc = nn.Sequential(
            nn.Linear(self.input_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
        )

        self.graph_1st = GatedGraph(self.note_hidden_size + self.measure_hidden_size * 2, self.num_edge_types, self.device, secondary_size=self.note_hidden_size)
        self.graph_between = nn.Sequential(
            nn.Linear(self.note_hidden_size + self.measure_hidden_size * 2, self.note_hidden_size + self.measure_hidden_size * 2),
            nn.Dropout(DROP_OUT),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.ReLU()
        )
        self.graph_2nd = GatedGraph(self.note_hidden_size + self.measure_hidden_size * 2, self.num_edge_types, self.device)

        self.measure_attention = nn.Linear(self.note_hidden_size * 2, self.note_hidden_size * 2)
        self.measure_rnn = nn.LSTM(self.note_hidden_size * 2, self.measure_hidden_size, self.num_measure_layers, batch_first=True, bidirectional=True)
        # self.tempo_attention = nn.Linear(self.output_size-1, self.output_size-1)

        self.performance_graph_encoder = GatedGraph(self.encoder_size, self.num_edge_types, self.device)
        self.performance_measure_attention = nn.Linear(self.encoder_size, self.encoder_size)

        self.performance_contractor = nn.Sequential(
            nn.Linear(self.encoder_input_size, self.encoder_size),
            nn.Dropout(DROP_OUT),
            # nn.BatchNorm1d(self.encoder_size),
            nn.ReLU(),
            nn.Linear(self.encoder_size, self.encoder_size),
            nn.Dropout(DROP_OUT),
            # nn.BatchNorm1d(self.encoder_size),
            nn.ReLU()
        )
        self.performance_encoder = nn.LSTM(self.encoder_size, self.encoder_size, num_layers=self.encoder_layer_num,
                                           batch_first=True, bidirectional=False)
        self.performance_encoder_mean = nn.Linear(self.encoder_size, self.encoder_size)
        self.performance_encoder_var = nn.Linear(self.encoder_size, self.encoder_size)

        self.beat_tempo_contractor = nn.Sequential(
            nn.Linear(self.final_input + self.encoder_size + self.output_size, self.time_regressive_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU()
        )

        self.perform_style_to_measure = nn.LSTM(self.measure_hidden_size * 2 + self.encoder_size, self.encoder_size, num_layers=1, bidirectional=False)

        self.initial_result_fc = nn.Linear(self.final_input, self.output_size)
        self.final_graph = GatedGraph(self.final_input + self.encoder_size + self.output_size, self.num_edge_types, self.device, self.output_size)
        self.tempo_rnn = nn.LSTM(self.time_regressive_size, self.time_regressive_size, num_layers=self.time_regressive_layer, batch_first=True, bidirectional=True)

        self.final_beat_attention = nn.Linear(self.final_input+self.encoder_size+self.output_size, self.final_input+self.encoder_size+self.output_size)
        self.tempo_fc = nn.Linear(self.time_regressive_size * 2, 1)
        # self.fc = nn.Linear(self.final_input + self.encoder_size + self.output_size, self.output_size - 1)
        self.fc = nn.Sequential(
            nn.Linear(self.final_input + self.encoder_size + self.output_size + self.time_regressive_size * 2 + 1, self.encoder_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),

            nn.Linear(self.encoder_size, self.output_size - 1),
        )

        self.softmax = nn.Softmax(dim=0)
        self.sigmoid = nn.Sigmoid()
        self.tanh = nn.Tanh()

    def forward(self, x, y, edges, note_locations, start_index, initial_z=False, return_z=False):
        beat_numbers = [x.beat for x in note_locations]
        measure_numbers = [x.measure for x in note_locations]
        num_notes = x.size(1)

        note_out, measure_hidden_out = self.run_graph_network(x, edges, measure_numbers, start_index)
        if type(initial_z) is not bool:
            if type(initial_z) is list or not initial_z.is_cuda:
                perform_z = torch.Tensor(initial_z).to(self.device).view(1,-1)
            else:
                perform_z = initial_z.view(1,-1)
            perform_mu = 0
            perform_var = 0
        else:
            encoder_hidden = self.init_performance_encoder(x.shape[0])
            perform_concat = torch.cat((note_out, y), 2).view(-1, self.encoder_input_size)
            perform_style_contracted = self.performance_contractor(perform_concat).view(1, num_notes, -1)
            perform_style_graphed = self.performance_graph_encoder(perform_style_contracted, edges)
            performance_measure_nodes = self.make_higher_node(perform_style_graphed, self.performance_measure_attention, beat_numbers,
                                                  measure_numbers, start_index, lower_is_note=True)
            perform_style_encoded, _ = self.performance_encoder(performance_measure_nodes, encoder_hidden)

            # perform_style_reduced = perform_style_reduced.view(-1,self.encoder_input_size)
            # perform_style_node = self.sum_with_attention(perform_style_reduced, self.perform_attention)
            perform_style_vector = perform_style_encoded[:, -1, :]  # need check
            perform_z, perform_mu, perform_var = \
                self.encode_with_net(perform_style_vector, self.performance_encoder_mean, self.performance_encoder_var)
        if return_z:
            return perform_z
        # perform_z = self.performance_decoder(perform_z)
        perform_z_batched = perform_z.repeat(num_notes, 1).view(1,num_notes, -1)

        # mean_velocity_info = x[:, :, mean_vel_start_index:mean_vel_start_index+4].view(1,-1,4)
        # dynamic_info = torch.cat((x[:, :, mean_vel_start_index + 4].view(1,-1,1),
        #                           x[:, :, vel_vec_start_index:vel_vec_start_index + 4]), 2).view(1,-1,5)

        # out_combined = torch.cat((
        #     note_out, measure_out_spanned), 2)

        # out = self.final_contractor(out_combined)
        initial_output = self.initial_result_fc(note_out)
        # initial_output = torch.zeros(1, num_notes, self.output_size).to(self.device)
        style_to_measure_hidden = self.init_performance_encoder(x.shape[0])
        num_measures = measure_numbers[start_index+num_notes-1] - measure_numbers[start_index] + 1
        perform_z_measure_spanned = perform_z.repeat(num_measures, 1).view(1,num_measures, -1)

        perform_z_measure_cat = torch.cat((perform_z_measure_spanned, measure_hidden_out), 2)
        measure_perform_style, _ = self.perform_style_to_measure(perform_z_measure_cat, style_to_measure_hidden)
        measure_perform_style_spanned = self.span_beat_to_note_num(measure_perform_style, measure_numbers, num_notes, start_index)
        out_with_result = torch.cat((note_out, measure_perform_style_spanned, initial_output), 2)
        tempo_hidden = self.init_beat_tempo_forward(x.shape[0])


        for i in range(5):
            out_with_result = self.final_graph(out_with_result, edges, iteration=self.graph_iteration)
            out_beat = self.make_higher_node(out_with_result, self.final_beat_attention, beat_numbers,
                                             beat_numbers, start_index, lower_is_note=True)
            out_beat = self.beat_tempo_contractor(out_beat)
            out_beat_rnn_result, _ = self.tempo_rnn(out_beat, tempo_hidden)
            tempo_out = self.tempo_fc(out_beat_rnn_result)
            tempos_spanned = self.span_beat_to_note_num(tempo_out, beat_numbers, num_notes, start_index)
            out_beat_spanned = self.span_beat_to_note_num(out_beat_rnn_result, beat_numbers, num_notes, start_index)
            out_with_beat_out = torch.cat((out_with_result, out_beat_spanned, tempos_spanned),2)
            other_out = self.fc(out_with_beat_out)

            final_out = torch.cat((tempos_spanned, other_out),2)
            out_with_result = torch.cat((out_with_result[:,:,:-self.output_size], final_out),2)
            # out = torch.cat((out, trill_out), 2)

        return final_out, perform_mu, perform_var, note_out

    def run_graph_network(self, nodes, adjacency_matrix, measure_numbers, start_index, iteration=5):
        # 1. Run feed-forward network by note level
        num_notes = nodes.shape[1]
        notes_hidden = self.note_fc(nodes)
        initial_measure = torch.zeros((notes_hidden.size(0), notes_hidden.size(1), self.measure_hidden_size * 2)).to(self.device)
        notes_and_measure_hidden = torch.cat((initial_measure, notes_hidden), 2)
        for i in range(iteration):
            notes_hidden = self.graph_1st(notes_and_measure_hidden, adjacency_matrix, iteration=self.graph_iteration)
            notes_between = self.graph_between(notes_hidden)
            notes_hidden_second = self.graph_2nd(notes_between, adjacency_matrix, iteration=self.graph_iteration)
            notes_hidden_cat = torch.cat((notes_hidden[:,:, -self.note_hidden_size:],
                                          notes_hidden_second[:,:, -self.note_hidden_size:]), -1)

            measure_nodes = self.make_higher_node(notes_hidden_cat, self.measure_attention, measure_numbers, measure_numbers,
                                                  start_index, lower_is_note=True)
            initial_measure_hidden = self.init_measure_layer(1)
            measure_hidden, _ = self.measure_rnn(measure_nodes, initial_measure_hidden)
            measure_hidden_spanned = self.span_beat_to_note_num(measure_hidden, measure_numbers, num_notes, start_index)
            notes_hidden = torch.cat((measure_hidden_spanned, notes_hidden[:,:,-self.note_hidden_size:]),-1)

        final_out = torch.cat((notes_hidden, notes_hidden_second),-1)
        return final_out, measure_hidden

    def encode_with_net(self, score_input, mean_net, var_net):
        mu = mean_net(score_input)
        var = var_net(score_input)

        z = self.reparameterize(mu, var)
        return z, mu, var

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5*logvar)
        eps = torch.randn_like(std)
        return eps.mul(std).add_(mu)

    # def decode_with_net(self, z, decode_network):
    #     decode_network
    #     return

    def sum_with_attention(self, hidden, attention_net):
        attention = attention_net(hidden)
        attention = self.softmax(attention)
        upper_node = hidden * attention
        upper_node_sum = torch.sum(upper_node, dim=0)

        return upper_node_sum

    def make_higher_node(self, lower_out, attention_weights, lower_indexes, higher_indexes, start_index, lower_is_note=False):
        higher_nodes = []
        prev_higher_index = higher_indexes[start_index]
        lower_node_start = 0
        lower_node_end = 0
        num_lower_nodes = lower_out.shape[1]
        start_lower_index = lower_indexes[start_index]
        lower_hidden_size = lower_out.shape[2]
        for low_index in range(num_lower_nodes):
            absolute_low_index = start_lower_index + low_index
            if lower_is_note:
                current_note_index = start_index + low_index
            else:
                current_note_index = lower_indexes.index(absolute_low_index)

            if higher_indexes[current_note_index] > prev_higher_index:
                # new beat start
                lower_node_end = low_index
                corresp_lower_out = lower_out[0, lower_node_start:lower_node_end, :]
                higher = self.sum_with_attention(corresp_lower_out, attention_weights)
                higher_nodes.append(higher)

                lower_node_start = low_index
                prev_higher_index = higher_indexes[current_note_index]

        corresp_lower_out = lower_out[0, lower_node_start:, :]
        higher = self.sum_with_attention(corresp_lower_out, attention_weights)
        higher_nodes.append(higher)

        higher_nodes = torch.stack(higher_nodes).view(1, -1, lower_hidden_size)

        return higher_nodes


    def span_beat_to_note_num(self, beat_out, beat_number, num_notes, start_index):
        start_beat = beat_number[start_index]
        num_beat = beat_out.shape[1]
        span_mat = torch.zeros(1, num_notes, num_beat)
        node_size = beat_out.shape[2]
        for i in range(num_notes):
            beat_index = beat_number[start_index+i] - start_beat
            if beat_index >= num_beat:
                beat_index = num_beat-1
            span_mat[0,i,beat_index] = 1
        span_mat = span_mat.to(self.device)

        spanned_beat = torch.bmm(span_mat, beat_out)
        return spanned_beat

    def note_tempo_infos_to_beat(self, y, beat_numbers, start_index, index=None):
        beat_tempos = []
        num_notes = y.size(1)
        prev_beat = -1
        for i in range(num_notes):
            cur_beat = beat_numbers[start_index+i]
            if cur_beat > prev_beat:
                if index is None:
                    beat_tempos.append(y[0,i,:])
                if index == TEMPO_IDX:
                    beat_tempos.append(y[0,i,TEMPO_IDX:TEMPO_IDX+5])
                else:
                    beat_tempos.append(y[0,i,index])
                prev_beat = cur_beat
        num_beats = len(beat_tempos)
        beat_tempos = torch.stack(beat_tempos).view(1,num_beats,-1)
        return beat_tempos

    def init_final_layer(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.final_hidden_size).to(self.device)
        return (h0, h0)

    def init_measure_layer(self, batch_size):
        h0 = torch.zeros(self.num_measure_layers * 2, batch_size, self.measure_hidden_size).to(self.device)
        return (h0, h0)

    def init_beat_tempo_forward(self, batch_size):
        h0 = torch.zeros(1 * 2, batch_size, self.time_regressive_size).to(self.device)
        return (h0, h0)

    def init_performance_encoder(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.encoder_size).to(self.device)


class TrillRNN(nn.Module):
    def __init__(self, network_parameters, trill_index, loss_type):
        super(TrillRNN, self).__init__()
        self.loss_type = loss_type
        self.hidden_size = network_parameters.note.size
        self.num_layers = network_parameters.note.layer
        self.input_size = network_parameters.input_size
        self.output_size = network_parameters.output_size
        self.is_trill_index = trill_index

        # self.lstm = nn.LSTM(self.input_size, self.hidden_size, self.num_layers, batch_first=True, bidirectional=True, dropout=DROP_OUT)
        # self.fc = nn.Linear(hidden_size * 2, num_output)  # 2 for bidirection
        self.fc = nn.Sequential(
            nn.Linear(self.hidden_size, self.hidden_size),
            nn.ReLU(),
            nn.Linear(self.hidden_size, self.hidden_size),
            nn.ReLU(),
            nn.Linear(self.hidden_size, self.output_size),
            nn.ReLU()
        )
        self.sigmoid = nn.Sigmoid()

    def forward(self, x, score_hidden):
        # hidden = self.init_hidden(x.size(0))
        # hidden_out, hidden = self.lstm(x, hidden)  # out: tensor of shape (batch_size, seq_length, hidden_size*2)

        # Decode the hidden state of the last time step
        score_hidden = torch.nn.Parameter(score_hidden, requires_grad=False)
        is_trill_mat = x[:, :, self.is_trill_index]
        is_trill_mat = is_trill_mat.view(1,-1,1).repeat(1,1,self.output_size).view(1,-1,self.output_size)
        is_trill_mat = Variable(is_trill_mat, requires_grad=False)
        out = self.fc(score_hidden)
        if self.loss_type == 'MSE':
            up_trill = self.sigmoid(out[:,:,-1])
            out[:,:,-1] = up_trill
        else:
            out = self.sigmoid(out)
        # out = out * is_trill_mat
        return out

    def init_hidden(self, batch_size):
        h0 = torch.zeros(self.num_layers * 2, batch_size, self.hidden_size).to(self.device)
        return (h0, h0)

class TrillGraph(nn.Module):
    def __init__(self, network_parameters, trill_index, loss_type, device):
        super(TrillGraph, self).__init__()
        self.loss_type = loss_type
        self.hidden_size = network_parameters.note.size
        self.num_layers = network_parameters.note.layer
        self.input_size = network_parameters.input_size
        self.output_size = network_parameters.output_size
        self.num_edge_types = network_parameters.num_edge_types
        self.is_trill_index = trill_index
        self.device = device

        # self.lstm = nn.LSTM(self.input_size, self.hidden_size, self.num_layers, batch_first=True, bidirectional=True, dropout=DROP_OUT)
        # self.fc = nn.Linear(hidden_size * 2, num_output)  # 2 for

        self.note_fc = nn.Sequential(
            nn.Linear(self.input_size, self.hidden_size),
            nn.ReLU(),
            nn.Dropout(DROP_OUT),
            nn.Linear(self.hidden_size, self.hidden_size),
            nn.ReLU(),
        )
        self.graph = GatedGraph(self.hidden_size, self.num_edge_types, self.device)

        self.out_fc = nn.Sequential(
            nn.Linear(self.hidden_size, self.hidden_size),
            nn.ReLU(),
            nn.Dropout(DROP_OUT),
            nn.Linear(self.hidden_size, self.output_size),
        )
        self.sigmoid = nn.Sigmoid()

    def forward(self, x, edges):
        # hidden = self.init_hidden(x.size(0))
        # hidden_out, hidden = self.lstm(x, hidden)  # out: tensor of shape (batch_size, seq_length, hidden_size*2)
        if edges.shape[0] != self.num_edge_types:
            edges = edges[:self.num_edge_types, :, :]

        # Decode the hidden state of the last time step
        is_trill_mat = x[:, :, self.is_trill_index]
        is_trill_mat = is_trill_mat.view(1,-1,1).repeat(1,1,self.output_size).view(1,-1,self.output_size)
        is_trill_mat = Variable(is_trill_mat, requires_grad=False)

        note_contracted = self.note_fc(x)
        note_after_graph = self.graph(note_contracted, edges, iteration=5)
        out = self.out_fc(note_after_graph)

        if self.loss_type == 'MSE':
            up_trill = self.sigmoid(out[:,:,-1])
            out[:,:,-1] = up_trill
        else:
            out = self.sigmoid(out)
        # out = out * is_trill_mat
        return out





class HAN_VAE(nn.Module):
    def __init__(self, network_parameters, device, step_by_step=False):
        super(HAN_VAE, self).__init__()
        self.device = device
        self.step_by_step = step_by_step
        self.input_size = network_parameters.input_size
        self.output_size = network_parameters.output_size
        self.num_layers = network_parameters.note.layer
        self.hidden_size = network_parameters.note.size
        self.num_beat_layers = network_parameters.beat.layer
        self.beat_hidden_size = network_parameters.beat.size
        self.num_measure_layers = network_parameters.measure.layer
        self.measure_hidden_size = network_parameters.measure.size
        self.final_hidden_size = network_parameters.final.size
        self.num_voice_layers = network_parameters.voice.layer
        self.voice_hidden_size = network_parameters.voice.size
        self.final_input = network_parameters.final.input
        self.encoder_size = network_parameters.encoder.size
        self.encoder_input_size = network_parameters.encoder.input
        self.encoder_layer_num = network_parameters.encoder.layer
        self.lstm = nn.LSTM(self.hidden_size, self.hidden_size, self.num_layers, batch_first=True, bidirectional=True, dropout=DROP_OUT)
        self.beat_attention = nn.Linear(self.hidden_size*2, self.hidden_size*2)
        self.beat_rnn = nn.LSTM(self.hidden_size * 2, self.beat_hidden_size, self.num_beat_layers, batch_first=True, bidirectional=True, dropout=DROP_OUT)
        self.measure_attention = nn.Linear(self.beat_hidden_size*2, self.beat_hidden_size*2)
        self.measure_rnn = nn.LSTM(self.beat_hidden_size * 2, self.measure_hidden_size, self.num_measure_layers, batch_first=True, bidirectional=True)
        # self.tempo_attention = nn.Linear(self.output_size-1, self.output_size-1)

        self.voice_net = nn.LSTM(self.hidden_size, self.voice_hidden_size, self.num_voice_layers, batch_first=True, bidirectional=True, dropout=DROP_OUT)

        self.note_fc = nn.Sequential(
            nn.Linear(self.input_size, self.hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
            nn.Linear(self.hidden_size, self.hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
            nn.Linear(self.hidden_size, self.hidden_size),
            # nn.BatchNorm1d(self.note_hidden_size),
            nn.Dropout(DROP_OUT),
            nn.ReLU(),
        )

        if self.step_by_step:
            self.beat_tempo_forward = nn.LSTM(self.beat_hidden_size*2 + 5 + 3 + self.output_size + self.encoder_size, self.beat_hidden_size, num_layers=1, batch_first=True, bidirectional=False)
            self.tempo_attention = nn.Linear(self.output_size - 1, self.output_size - 1)
        else:
            self.beat_tempo_forward = nn.LSTM(self.beat_hidden_size*2 + 3+ 3 + self.encoder_size, self.beat_hidden_size, num_layers=1, batch_first=True, bidirectional=False)
        self.beat_tempo_fc = nn.Linear(self.beat_hidden_size,  1)

        self.output_lstm = nn.LSTM(self.final_input, self.final_hidden_size, num_layers=1, batch_first=True, bidirectional=False)
        self.fc = nn.Linear(self.final_hidden_size, self.output_size - 1)

        self.performance_note_encoder = nn.LSTM(self.encoder_size, self.encoder_size)
        self.performance_measure_attention = nn.Linear(self.encoder_size, self.encoder_size)

        self.performance_contractor = nn.Sequential(
            nn.Linear(self.encoder_input_size, self.encoder_size),
            nn.Dropout(DROP_OUT),
            # nn.BatchNorm1d(self.encoder_size),
            nn.ReLU()
        )
        self.performance_encoder = nn.LSTM(self.encoder_size, self.encoder_size,  num_layers=self.encoder_layer_num, batch_first=True, bidirectional=False)
        self.performance_encoder_mean = nn.Linear(self.encoder_size, self.encoder_size)
        self.performance_encoder_var = nn.Linear(self.encoder_size, self.encoder_size)

        self.softmax = nn.Softmax(dim=0)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x, y, edges, note_locations, start_index, initial_z=False, rand_threshold=0.7):
        beat_numbers = [x.beat for x in note_locations]
        measure_numbers = [x.measure for x in note_locations]
        voice_numbers = [x.voice for x in note_locations]
        onset_numbers = [x.onset for x in note_locations]
        num_notes = x.size(1)
        note_out, beat_hidden_out, measure_hidden_out, voice_out = \
            self.run_offline_score_model(x, onset_numbers, beat_numbers, measure_numbers, voice_numbers, start_index)
        beat_out_spanned = self.span_beat_to_note_num(beat_hidden_out, beat_numbers, num_notes, start_index)
        measure_out_spanned = self.span_beat_to_note_num(measure_hidden_out, measure_numbers, num_notes, start_index)
        if initial_z:
            perform_z = torch.Tensor(initial_z).to(self.device).view(1,-1)
            perform_mu = 0
            perform_var = 0
        else:
            perform_concat = torch.cat((note_out, beat_out_spanned, measure_out_spanned, voice_out, y), 2)
            perform_contracted = self.performance_contractor(perform_concat)
            perform_note_encoded, _ = self.performance_note_encoder(perform_contracted)

            perform_measure = self.make_higher_node(perform_note_encoded, self.performance_measure_attention, beat_numbers,
                                                    measure_numbers, start_index, lower_is_note=True)
            perform_style_encoded, _ = self.performance_encoder(perform_measure)
            # perform_style_reduced = perform_style_reduced.view(-1,self.encoder_input_size)
            # perform_style_node = self.sum_with_attention(perform_style_reduced, self.perform_attention)
            perform_style_vector = perform_style_encoded[:, -1, :]  # need check
            perform_z, perform_mu, perform_var = \
                self.encode_with_net(perform_style_vector, self.performance_encoder_mean, self.performance_encoder_var)

        # perform_z = self.performance_decoder(perform_z)
        perform_z_batched = perform_z.repeat(x.shape[1], 1).view(1,x.shape[1], -1)
        perform_z = perform_z.view(-1)

        tempo_hidden = self.init_beat_tempo_forward(x.size(0))
        final_hidden = self.init_final_layer(x.size(0))

        num_beats = beat_hidden_out.size(1)

        if self.step_by_step:
            num_beats = beat_hidden_out.size(1)
            qpm_primo = x[:, 0, QPM_PRIMO_IDX]
            tempo_primo = x[0, 0, TEMPO_PRIMO_IDX:]
            max_voice = max(voice_numbers[start_index:start_index + num_notes])
            vel_by_voice = [torch.zeros(NUM_VOICE_FEED_PARAM).to(self.device)] * max_voice

            prev_out = y[0, 0, :]
            prev_tempo = y[:, 0, QPM_INDEX]
            prev_beat = -1
            prev_beat_end = 0
            out_total = torch.zeros(num_notes, self.output_size).to(self.device)
            result_nodes = torch.zeros(num_beats, self.output_size - 1).to(self.device)
            prev_out_list = []
            # if args.beatTempo:
            #     prev_out[0] = tempos_spanned[0, 0, 0]
            has_ground_truth = y.size(1) > 1
            if has_ground_truth:
                true_prev_tempos = self.note_tempo_infos_to_beat(y, beat_numbers, start_index, QPM_INDEX)
            for i in range(num_notes):
                current_beat = beat_numbers[start_index + i] - beat_numbers[start_index]
                if current_beat > prev_beat:  # beat changed
                    if i - prev_beat_end > 0:  # if there are outputs to consider
                        corresp_result = torch.stack(prev_out_list)
                    else:  # there is no previous output
                        corresp_result = y[0, 0, 1:]

                    result_node = self.sum_with_attention(corresp_result, self.tempo_attention)
                    prev_out_list = []
                    result_nodes[current_beat, :] = result_node

                    tempos = torch.zeros(1, num_beats, 1).to(self.device)
                    beat_tempo_vec = x[0, i, TEMPO_IDX:TEMPO_IDX + 5]
                    beat_tempo_cat = torch.cat((beat_hidden_out[0, current_beat, :], prev_tempo,
                                                qpm_primo, tempo_primo, beat_tempo_vec,
                                                result_nodes[current_beat, :], perform_z)).view(1, 1, -1)
                    beat_forward, tempo_hidden = self.beat_tempo_forward(beat_tempo_cat, tempo_hidden)
                    tmp_tempos = self.beat_tempo_fc(beat_forward)

                    prev_beat_end = i
                    prev_tempo = tmp_tempos.view(1)
                    prev_beat = current_beat

                tmp_voice = voice_numbers[start_index + i] - 1
                # if has_ground_truth and random.random() > rand_threshold:
                corresp_beat = beat_numbers[start_index + i] - beat_numbers[start_index]
                corresp_measure = measure_numbers[start_index + i] - measure_numbers[start_index]
                prev_voice_vel = vel_by_voice[tmp_voice]
                # dynamic_info = torch.cat((x[:,i,mean_vel_start_index+4], x[0, i,vel_vec_start_index:vel_vec_start_index+5] ))
                out_combined = torch.cat(
                    (note_out[0, i, :], beat_hidden_out[0, corresp_beat, :],
                     measure_hidden_out[0, corresp_measure, :], voice_out[0, i, :],
                     prev_out, prev_voice_vel, qpm_primo, tempo_primo, perform_z)).view(1, 1, -1)

                out, final_hidden = self.output_lstm(out_combined, final_hidden)
                # out = torch.cat((out, out_combined), 2)
                out = out.view(-1)
                out = self.fc(out)

                prev_out_list.append(out)
                out = torch.cat((prev_tempo, out))

                prev_out = out
                vel_by_voice[tmp_voice] = out[1:1 + NUM_VOICE_FEED_PARAM].view(-1)
                out_total[i, :] = out

            out_total = out_total.view(1, num_notes, -1)
            hidden_total = torch.cat((note_out, beat_out_spanned, measure_out_spanned, voice_out), 2)
            return out_total, perform_mu, perform_var, hidden_total
        else:
            # non autoregressive
            qpm_primo = x[:,:,QPM_PRIMO_IDX].view(1,-1,1)
            tempo_primo = x[:,:,TEMPO_PRIMO_IDX:].view(1,-1,2)
            # beat_tempos = self.note_tempo_infos_to_beat(y, beat_numbers, start_index, QPM_INDEX)
            beat_qpm_primo = qpm_primo[0,0,0].repeat((1, num_beats, 1))
            beat_tempo_primo = tempo_primo[0,0,:].repeat((1, num_beats, 1))
            beat_tempo_vector = self.note_tempo_infos_to_beat(x, beat_numbers, start_index, TEMPO_IDX)
            if 'beat_hidden_out' not in locals():
                beat_hidden_out = beat_out_spanned
            num_beats = beat_hidden_out.size(1)
            # score_z_beat_spanned = score_z.repeat(num_beats,1).view(1,num_beats,-1)
            perform_z_beat_spanned = perform_z.repeat(num_beats,1).view(1,num_beats,-1)
            beat_tempo_cat = torch.cat((beat_hidden_out, beat_qpm_primo, beat_tempo_primo, beat_tempo_vector, perform_z_beat_spanned), 2)
            beat_forward, tempo_hidden = self.beat_tempo_forward(beat_tempo_cat, tempo_hidden)
            tempos = self.beat_tempo_fc(beat_forward)
            num_notes = note_out.size(1)
            tempos_spanned = self.span_beat_to_note_num(tempos, beat_numbers, num_notes, start_index)
            # y[0, :, 0] = tempos_spanned.view(-1)



            # mean_velocity_info = x[:, :, mean_vel_start_index:mean_vel_start_index+4].view(1,-1,4)
            # dynamic_info = torch.cat((x[:, :, mean_vel_start_index + 4].view(1,-1,1),
            #                           x[:, :, vel_vec_start_index:vel_vec_start_index + 4]), 2).view(1,-1,5)

            out_combined = torch.cat((
                note_out, beat_out_spanned, measure_out_spanned,
                # qpm_primo, tempo_primo, mean_velocity_info, dynamic_info,
                voice_out, perform_z_batched), 2)

            out, final_hidden = self.output_lstm(out_combined, final_hidden)

            out = self.fc(out)
            # out = torch.cat((out, trill_out), 2)

            out = torch.cat((tempos_spanned, out), 2)
            score_combined = torch.cat((
                note_out, beat_out_spanned, measure_out_spanned,
                voice_out), 2)

            return out, perform_mu, perform_var, score_combined

    def run_offline_score_model(self, x, onset_numbers, beat_numbers, measure_numbers, voice_numbers, start_index):
        hidden = self.init_hidden(x.size(0))
        beat_hidden = self.init_beat_layer(x.size(0))
        measure_hidden = self.init_measure_layer(x.size(0))

        x = self.note_fc(x)

        temp_voice_numbers = voice_numbers[start_index:start_index + x.size(1)]
        if temp_voice_numbers == []:
            temp_voice_numbers = voice_numbers[start_index:]
        max_voice = max(temp_voice_numbers)
        voice_hidden = self.init_voice_layer(1, max_voice)
        voice_out, voice_hidden = self.run_voice_net(x, voice_hidden, temp_voice_numbers, max_voice)

        # note_out, onset_out = self.run_onset_rnn(x, voice_out, onset_numbers, start_index)
        hidden_out, hidden = self.lstm(x, hidden)  # out: tensor of shape (batch_size, seq_length, hidden_size*2)
        # beat_nodes = self.make_higher_node(onset_out, self.beat_attention, onset_numbers, beat_numbers, start_index)
        beat_nodes = self.make_beat_node(hidden_out, beat_numbers, start_index)
        beat_hidden_out, beat_hidden = self.beat_rnn(beat_nodes, beat_hidden)
        measure_nodes = self.make_higher_node(beat_hidden_out, self.measure_attention, beat_numbers, measure_numbers, start_index)
        # measure_nodes = self.make_measure_node(beat_hidden_out, measure_numbers, beat_numbers, start_index)
        measure_hidden_out, measure_hidden = self.measure_rnn(measure_nodes, measure_hidden)

        return hidden_out, beat_hidden_out, measure_hidden_out, voice_out

    def run_onset_rnn(self, input_notes, voice_outputs, onset_numbers, start_index):
        note_nodes = []
        onset_nodes = []
        prev_onset = onset_numbers[start_index]
        onset_notes_start = 0
        beat_notes_end = 0
        num_notes = input_notes.shape[1]
        for note_index in range(num_notes):
            abs_index = start_index + note_index
            if onset_numbers[abs_index] > prev_onset:
                # new beat start or sequence ends
                onset_notes_end = note_index
                corresp_notes = input_notes[:, onset_notes_start:onset_notes_end, :]
                corresp_voice_hiden = voice_outputs[:, onset_notes_start:onset_notes_end,:]

                note_hidden = self.init_hidden(1)
                onset_encoder_hidden = self.init_onset_encoder(1)

                note_output, note_hidden = self.lstm(corresp_notes, note_hidden)
                note_concated = torch.cat((note_output, corresp_voice_hiden), 2)
                encoded_onset, _ = self.onset_encoder(note_concated, onset_encoder_hidden)

                onset = encoded_onset[0,-1,:]
                onset_nodes.append(onset)
                note_nodes.append(note_output)

                onset_notes_start = note_index
                prev_onset = onset_numbers[abs_index]

        corresp_notes = input_notes[:, onset_notes_start:, :]
        corresp_voice_hiden = voice_outputs[:, onset_notes_start:, :]

        note_hidden = self.init_hidden(1)
        onset_encoder_hidden = self.init_onset_encoder(1)

        note_output, note_hidden = self.lstm(corresp_notes, note_hidden)
        note_concated = torch.cat((note_output, corresp_voice_hiden), 2)
        encoded_onset, _ = self.onset_encoder(note_concated, onset_encoder_hidden)

        onset = encoded_onset[0, -1, :]
        onset_nodes.append(onset)
        note_nodes.append(note_output)

        onset_nodes = torch.stack(onset_nodes).view(1, -1, self.onset_hidden_size)
        note_out = torch.cat(note_nodes, 1)

        onset_hidden = self.init_onset_layer(1)
        onset_out, onset_hidden = self.onset_rnn(onset_nodes, onset_hidden)

        return note_out, onset_out


    def encode_with_net(self, score_input, mean_net, var_net):
        mu = mean_net(score_input)
        var = var_net(score_input)

        z = self.reparameterize(mu, var)
        return z, mu, var

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5*logvar)
        eps = torch.randn_like(std)
        return eps.mul(std).add_(mu)

    # def decode_with_net(self, z, decode_network):
    #     decode_network
    #     return

    def sum_with_attention(self, hidden, attention_net):
        attention = attention_net(hidden)
        attention = self.softmax(attention)
        upper_node = hidden * attention
        upper_node_sum = torch.sum(upper_node, dim=0)

        return upper_node_sum

    def make_higher_node(self, lower_out, attention_weights, lower_indexes, higher_indexes, start_index, lower_is_note=False):
        higher_nodes = []
        prev_higher_index = higher_indexes[start_index]
        lower_node_start = 0
        lower_node_end = 0
        num_lower_nodes = lower_out.shape[1]
        start_lower_index = lower_indexes[start_index]
        lower_hidden_size = lower_out.shape[2]
        for low_index in range(num_lower_nodes):
            absolute_low_index = start_lower_index + low_index
            if lower_is_note:
                current_note_index = start_index + low_index
            else:
                current_note_index = lower_indexes.index(absolute_low_index)

            if higher_indexes[current_note_index] > prev_higher_index:
                # new beat start
                lower_node_end = low_index
                corresp_lower_out = lower_out[0, lower_node_start:lower_node_end, :]
                higher = self.sum_with_attention(corresp_lower_out, attention_weights)
                higher_nodes.append(higher)

                lower_node_start = low_index
                prev_higher_index = higher_indexes[current_note_index]

        corresp_lower_out = lower_out[0, lower_node_start:, :]
        higher = self.sum_with_attention(corresp_lower_out, attention_weights)
        higher_nodes.append(higher)

        higher_nodes = torch.stack(higher_nodes).view(1, -1, lower_hidden_size)

        return higher_nodes


    def make_beat_node(self, hidden_out, beat_number, start_index):
        beat_nodes = []
        prev_beat = beat_number[start_index]
        beat_notes_start = 0
        beat_notes_end = 0
        num_notes = hidden_out.shape[1]
        for note_index in range(num_notes):
            actual_index = start_index + note_index
            if beat_number[actual_index] > prev_beat:
                #new beat start
                beat_notes_end = note_index
                corresp_hidden = hidden_out[0, beat_notes_start:beat_notes_end, :]
                beat = self.sum_with_attention(corresp_hidden, self.beat_attention)
                beat_nodes.append(beat)

                beat_notes_start = note_index
                prev_beat = beat_number[actual_index]

        last_hidden =  hidden_out[0, beat_notes_end:, :]
        beat = self.sum_with_attention(last_hidden, self.beat_attention)
        beat_nodes.append(beat)

        beat_nodes = torch.stack(beat_nodes).view(1,-1,self.hidden_size*2)
        # beat_nodes = torch.Tensor(beat_nodes)

        return beat_nodes

    def make_measure_node(self, beat_out, measure_number, beat_number, start_index):
        measure_nodes = []
        prev_measure = measure_number[start_index]
        measure_beats_start = 0
        measure_beats_end = 0
        num_beats = beat_out.shape[1]
        start_beat = beat_number[start_index]
        for beat_index in range(num_beats):
            current_beat = start_beat + beat_index
            current_note_index = beat_number.index(current_beat)

            if measure_number[current_note_index] > prev_measure:
                # new beat start
                measure_beats_end = beat_index
                corresp_hidden = beat_out[0, measure_beats_start:measure_beats_end, :]
                measure = self.sum_with_attention(corresp_hidden, self.measure_attention)
                measure_nodes.append(measure)

                measure_beats_start = beat_index
                prev_measure = measure_number[beat_index]

        last_hidden = beat_out[0, measure_beats_end:, :]
        measure = self.sum_with_attention(last_hidden, self.measure_attention)
        measure_nodes.append(measure)

        measure_nodes = torch.stack(measure_nodes).view(1,-1,self.beat_hidden_size*2)

        return measure_nodes

    def span_beat_to_note_num(self, beat_out, beat_number, num_notes, start_index):
        start_beat = beat_number[start_index]
        num_beat = beat_out.shape[1]
        span_mat = torch.zeros(1, num_notes, num_beat)
        node_size = beat_out.shape[2]
        for i in range(num_notes):
            beat_index = beat_number[start_index+i] - start_beat
            if beat_index >= num_beat:
                beat_index = num_beat-1
            span_mat[0,i,beat_index] = 1
        span_mat = span_mat.to(self.device)

        spanned_beat = torch.bmm(span_mat, beat_out)
        return spanned_beat

    def note_tempo_infos_to_beat(self, y, beat_numbers, start_index, index=None):
        beat_tempos = []
        num_notes = y.size(1)
        prev_beat = -1
        for i in range(num_notes):
            cur_beat = beat_numbers[start_index+i]
            if cur_beat > prev_beat:
                if index is None:
                    beat_tempos.append(y[0,i,:])
                if index == TEMPO_IDX:
                    beat_tempos.append(y[0,i,TEMPO_IDX:TEMPO_IDX+5])
                else:
                    beat_tempos.append(y[0,i,index])
                prev_beat = cur_beat
        num_beats = len(beat_tempos)
        beat_tempos = torch.stack(beat_tempos).view(1,num_beats,-1)
        return beat_tempos


    def run_voice_net(self, batch_x, voice_hidden, voice_numbers, max_voice):
        num_notes = batch_x.size(1)
        output = torch.zeros(1, batch_x.size(1), self.voice_hidden_size * 2).to(self.device)
        voice_numbers = torch.Tensor(voice_numbers)
        for i in range(1,max_voice+1):
            voice_x_bool = voice_numbers == i
            num_voice_notes = torch.sum(voice_x_bool)
            if num_voice_notes > 0:
                span_mat = torch.zeros(num_notes, num_voice_notes)
                note_index_in_voice = 0
                for j in range(num_notes):
                    if voice_x_bool[j] ==1:
                        span_mat[j, note_index_in_voice] = 1
                        note_index_in_voice += 1
                span_mat = span_mat.view(1,num_notes,-1).to(self.device)
                voice_x = batch_x[0,voice_x_bool,:].view(1,-1, self.hidden_size)
                ith_hidden = voice_hidden[i-1]

                ith_voice_out, ith_hidden = self.voice_net(voice_x, ith_hidden)
                # ith_voice_out, ith_hidden = self.lstm(voice_x, ith_hidden)
                output += torch.bmm(span_mat, ith_voice_out)
        return output, voice_hidden

    def init_hidden(self, batch_size):
        h0 = torch.zeros(self.num_layers * 2, batch_size, self.hidden_size).to(self.device)
        return (h0, h0)

    def init_final_layer(self, batch_size):
        h0 = torch.zeros(1 , batch_size, self.final_hidden_size).to(self.device)
        return (h0, h0)

    def init_onset_layer(self, batch_size):
        h0 = torch.zeros(self.num_onset_layers * 2, batch_size, self.onset_hidden_size).to(self.device)
        return (h0, h0)

    def init_beat_layer(self, batch_size):
        h0 = torch.zeros(self.num_beat_layers * 2, batch_size, self.beat_hidden_size).to(self.device)
        return (h0, h0)

    def init_measure_layer(self, batch_size):
        h0 = torch.zeros(self.num_measure_layers * 2, batch_size, self.measure_hidden_size).to(self.device)
        return (h0, h0)

    def init_beat_tempo_forward(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.beat_hidden_size).to(self.device)
        return (h0, h0)

    def init_voice_layer(self, batch_size, max_voice):
        layers = []
        for i in range(max_voice):
            # h0 = torch.zeros(self.num_voice_layers * 2, batch_size, self.voice_hidden_size).to(device)
            h0 = torch.zeros(self.num_voice_layers * 2, batch_size, self.hidden_size).to(self.device)
            layers.append((h0, h0))
        return layers

    def init_onset_encoder(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.onset_hidden_size).to(self.device)
        return (h0, h0)






class HAN(nn.Module):
    def __init__(self, network_parameters, device, num_trill_param=5):
        super(HAN, self).__init__()
        self.device = device
        self.input_size = network_parameters.input_size
        self.output_size = network_parameters.output_size
        self.note_layer_num = network_parameters.note.layer
        self.note_hidden_size = network_parameters.note.size
        self.beat_layer_num = network_parameters.beat.layer
        self.beat_hidden_size = network_parameters.beat.size
        self.measure_layer_num = network_parameters.measure.layer
        self.measure_hidden_size = network_parameters.measure.size
        self.unidir_layer_num = network_parameters.final.layer
        self.unidir_hidden_size = network_parameters.final.size
        self.num_voice_layers = network_parameters.voice.layer
        self.num_voice_layers = network_parameters.note.layer
        self.voice_hidden_size = network_parameters.note.size
        self.summarize_layers = network_parameters.sum.layer
        self.summarize_size = network_parameters.sum.size
        self.unidir_input_size = network_parameters.final.input
        self.encoder_size = network_parameters.encoder.size
        self.encoder_input_size = network_parameters.encoder.input
        self.encoder_layer_num = network_parameters.encoder.layer

        self.lstm = nn.LSTM(self.input_size, self.note_hidden_size,
                            self.note_layer_num, batch_first=True, bidirectional=True, dropout=DROP_OUT)
        self.output_lstm = nn.LSTM(self.unidir_input_size, self.unidir_hidden_size, num_layers=self.unidir_layer_num, batch_first=True, bidirectional=False)
        # if args.trainTrill:
        #     self.output_lstm = nn.LSTM((self.hidden_size + self.beat_hidden_size + self.measure_hidden_size) *2 + num_output + num_tempo_info,
        #                                self.final_hidden_size, num_layers=1, batch_first=True, bidirectional=False)
        # else:
        #     self.output_lstm = nn.LSTM(
        #         (self.hidden_size + self.beat_hidden_size + self.measure_hidden_size) * 2 + num_output - num_trill_param + num_tempo_info,
        #         self.final_hidden_size, num_layers=1, batch_first=True, bidirectional=False)
        self.beat_attention = nn.Linear(self.note_hidden_size * 2, self.note_hidden_size * 2)
        self.beat_hidden = nn.LSTM(self.note_hidden_size * 2, self.beat_hidden_size, self.beat_layer_num, batch_first=True, bidirectional=True, dropout=DROP_OUT)
        self.measure_attention = nn.Linear(self.beat_hidden_size*2, self.beat_hidden_size*2)
        self.measure_hidden = nn.LSTM(self.beat_hidden_size * 2, self.measure_hidden_size, self.measure_layer_num, batch_first=True, bidirectional=True)
        self.tempo_attention = nn.Linear(self.output_size-1, self.output_size-1)
        self.fc = nn.Linear(self.unidir_hidden_size, self.output_size - 1)
        self.softmax = nn.Softmax(dim=0)
        self.sigmoid = nn.Sigmoid()
        self.beat_tempo_forward = nn.LSTM(self.beat_hidden_size*2+1+3+3+self.output_size-1, self.beat_hidden_size, num_layers=1, batch_first=True, bidirectional=False)
        self.beat_tempo_fc = nn.Sequential(
            nn.Linear(self.beat_hidden_size, self.beat_hidden_size),
            nn.ReLU(),
            nn.Dropout(DROP_OUT),

            nn.Linear(self.beat_hidden_size, 1))
        self.voice_net = nn.LSTM(self.input_size, self.voice_hidden_size, self.num_voice_layers, batch_first=True, bidirectional=True, dropout=DROP_OUT)

        self.performance_note_encoder = nn.LSTM(self.encoder_size, self.encoder_size)
        self.performance_measure_attention = nn.Linear(self.encoder_size, self.encoder_size)

        self.performance_contractor = nn.Sequential(
            nn.Linear(self.encoder_input_size, self.encoder_size),
            nn.Dropout(DROP_OUT),
            # nn.BatchNorm1d(self.encoder_size),
            nn.ReLU()
        )
        self.performance_encoder = nn.LSTM(self.encoder_size, self.encoder_size, num_layers=self.encoder_layer_num,
                                           batch_first=True, bidirectional=False)
        self.performance_encoder_mean = nn.Linear(self.encoder_size, self.encoder_size)
        self.performance_encoder_var = nn.Linear(self.encoder_size, self.encoder_size)

    def forward(self, x, y, edges, note_locations, start_index, initial_z=False, rand_threshold=0.7):
        beat_numbers = [x.beat for x in note_locations]
        measure_numbers = [x.measure for x in note_locations]
        voice_numbers = [x.voice for x in note_locations]
        num_notes = x.size(1)
        note_out, beat_hidden_out, measure_hidden_out, voice_out = \
            self.run_offline_score_model(x, beat_numbers, measure_numbers, voice_numbers, start_index)
        beat_out_spanned = self.span_beat_to_note_num(beat_hidden_out, beat_numbers, num_notes, start_index)
        measure_out_spanned = self.span_beat_to_note_num(measure_hidden_out, measure_numbers, num_notes, start_index)

        # perform_z = self.performance_decoder(perform_z)
        tempo_hidden = self.init_beat_tempo_forward(x.size(0))
        final_hidden = self.init_final_layer(x.size(0))

#        if step_by_step:
        num_beats = beat_hidden_out.size(1)
        qpm_primo = x[:, 0, QPM_PRIMO_IDX]
        tempo_primo = x[0, 0, TEMPO_PRIMO_IDX:]
        max_voice = max(voice_numbers[start_index:start_index+num_notes])
        vel_by_voice = [torch.zeros(NUM_VOICE_FEED_PARAM).to(self.device)] * max_voice

        prev_out = y[0, 0, :]
        prev_tempo = y[:, 0, QPM_INDEX]
        prev_beat = -1
        prev_beat_end = 0
        out_total = torch.zeros(num_notes,self.output_size).to(self.device)
        result_nodes = torch.zeros(num_beats, self.output_size-1).to(self.device)
        prev_out_list = []
        # if args.beatTempo:
        #     prev_out[0] = tempos_spanned[0, 0, 0]
        has_ground_truth = y.size(1) > 1
        if has_ground_truth:
            true_tempos = self.note_tempo_infos_to_beat(y, beat_numbers, start_index, QPM_INDEX)
        for i in range(num_notes):
            current_beat = beat_numbers[start_index+ i] - beat_numbers[start_index]
            if current_beat > prev_beat:  # beat changed
                # use true previous state by coin flip
                # if has_ground_truth and random.random() > rand_threshold:
                if has_ground_truth and current_beat > 0 and random.random() > rand_threshold:
                    # use true previous status
                    prev_tempos = true_tempos[:, current_beat-1, QPM_INDEX]
                    number_of_prev_notes = len(prev_out_list)
                    corresp_result = y[0, i-number_of_prev_notes-1:i, 1:]
                # sum up the output features of the previous beat
                else:
                    if i - prev_beat_end > 0:  # if there are outputs to consider
                        corresp_result = torch.stack(prev_out_list)
                    else:  # there is no previous output
                        corresp_result = y[0, 0, 1:]

                result_node = self.sum_with_attention(corresp_result, self.tempo_attention)
                prev_out_list = []
                result_nodes[current_beat, :] = result_node

                beat_tempo_vec = x[0, i, TEMPO_IDX:TEMPO_IDX + 5]
                beat_tempo_cat = torch.cat((beat_hidden_out[0,current_beat,:], prev_tempo,
                                        qpm_primo, tempo_primo, beat_tempo_vec, result_nodes[current_beat,:])).view(1, 1, -1)
                beat_forward, tempo_hidden = self.beat_tempo_forward(beat_tempo_cat, tempo_hidden)
                tmp_tempos = self.beat_tempo_fc(beat_forward)

                prev_beat_end = i
                prev_tempo = tmp_tempos.view(1)
                prev_beat = current_beat

            tmp_voice = voice_numbers[start_index + i] - 1
            # if has_ground_truth and random.random() > rand_threshold:
            if has_ground_truth and i > 0 and current_beat + 1 < true_tempos.shape[1] and random.random() > rand_threshold:
                prev_out = y[0, i-1, 1:]
                true_current_tempo = true_tempos[0, current_beat, :]
                prev_out = torch.cat( (true_current_tempo, prev_out))

            corresp_beat = beat_numbers[start_index+i] - beat_numbers[start_index]
            corresp_measure = measure_numbers[start_index + i] - measure_numbers[start_index]
            prev_voice_vel = vel_by_voice[tmp_voice]
            # dynamic_info = torch.cat((x[:,i,mean_vel_start_index+4], x[0, i,vel_vec_start_index:vel_vec_start_index+5] ))
            out_combined = torch.cat(
                (note_out[0,i,:], beat_hidden_out[0,corresp_beat,:],
                 measure_hidden_out[0,corresp_measure,:], voice_out[0,i,:],
                 prev_out, prev_voice_vel, qpm_primo, tempo_primo)).view(1,1,-1)

            out, final_hidden = self.output_lstm(out_combined, final_hidden)
            # out = torch.cat((out, out_combined), 2)
            out = out.view(-1)
            out = self.fc(out)

            prev_out_list.append(out)
            out = torch.cat((prev_tempo, out))

            prev_out = out
            vel_by_voice[tmp_voice] = out[1:1 + NUM_VOICE_FEED_PARAM].view(-1)
            out_total[i,:] = out

        out_total = out_total.view(1, num_notes, -1)
        hidden_total = torch.cat((note_out, beat_out_spanned, measure_out_spanned, voice_out),2)
        return out_total, False, False, hidden_total


    def run_offline_score_model(self, x, beat_numbers, measure_numbers, voice_numbers, start_index):
        hidden = self.init_hidden(x.size(0))
        beat_hidden = self.init_beat_layer(x.size(0))
        measure_hidden = self.init_measure_layer(x.size(0))

        hidden_out, hidden = self.lstm(x, hidden)  # out: tensor of shape (batch_size, seq_length, hidden_size*2)
        beat_nodes = self.make_beat_node(hidden_out, beat_numbers, start_index)
        beat_hidden_out, beat_hidden = self.beat_hidden(beat_nodes, beat_hidden)
        measure_nodes = self.make_measure_node(beat_hidden_out, measure_numbers, beat_numbers, start_index)
        measure_hidden_out, measure_hidden = self.measure_hidden(measure_nodes, measure_hidden)

        temp_voice_numbers = voice_numbers[start_index:start_index + x.size(1)]
        if temp_voice_numbers == []:
            temp_voice_numbers = voice_numbers[start_index:]
        max_voice = max(temp_voice_numbers)
        voice_hidden = self.init_voice_layer(1, max_voice)
        voice_out, voice_hidden = self.run_voice_net(x, voice_hidden, temp_voice_numbers, max_voice)

        return hidden_out, beat_hidden_out, measure_hidden_out, voice_out

    def sum_with_attention(self, hidden, attention_net):
        if len(hidden.shape) == 1:
            return hidden
        attention = attention_net(hidden)
        attention = self.softmax(attention)
        upper_node = hidden * attention
        upper_node_sum = torch.sum(upper_node, dim=0)

        return upper_node_sum

    def make_higher_node(self, lower_out, attention_weights, lower_indexes, higher_indexes, start_index, lower_is_note=False):
        higher_nodes = []
        prev_higher_index = higher_indexes[start_index]
        lower_node_start = 0
        lower_node_end = 0
        num_lower_nodes = lower_out.shape[1]
        start_lower_index = lower_indexes[start_index]
        lower_hidden_size = lower_out.shape[2]
        for low_index in range(num_lower_nodes):
            absolute_low_index = start_lower_index + low_index
            if lower_is_note:
                current_note_index = start_index + low_index
            else:
                current_note_index = lower_indexes.index(absolute_low_index)

            if higher_indexes[current_note_index] > prev_higher_index:
                # new beat start
                lower_node_end = low_index
                corresp_lower_out = lower_out[0, lower_node_start:lower_node_end, :]
                higher = self.sum_with_attention(corresp_lower_out, attention_weights)
                higher_nodes.append(higher)

                lower_node_start = low_index
                prev_higher_index = higher_indexes[current_note_index]

        corresp_lower_out = lower_out[0, lower_node_start:, :]
        higher = self.sum_with_attention(corresp_lower_out, attention_weights)
        higher_nodes.append(higher)

        higher_nodes = torch.stack(higher_nodes).view(1, -1, lower_hidden_size)

        return higher_nodes

    def make_beat_node(self, hidden_out, beat_number, start_index):
        beat_nodes = []
        prev_beat = beat_number[start_index]
        beat_notes_start = 0
        beat_notes_end = 0
        num_notes = hidden_out.shape[1]
        for note_index in range(num_notes):
            actual_index = start_index + note_index
            if beat_number[actual_index] > prev_beat:
                #new beat start
                beat_notes_end = note_index
                corresp_hidden = hidden_out[0, beat_notes_start:beat_notes_end, :]
                beat = self.sum_with_attention(corresp_hidden, self.beat_attention)
                beat_nodes.append(beat)

                beat_notes_start = note_index
                prev_beat = beat_number[actual_index]

        last_hidden =  hidden_out[0, beat_notes_end:, :]
        beat = self.sum_with_attention(last_hidden, self.beat_attention)
        beat_nodes.append(beat)

        beat_nodes = torch.stack(beat_nodes).view(1, -1, self.note_hidden_size * 2)
        # beat_nodes = torch.Tensor(beat_nodes)

        return beat_nodes

    def make_measure_node(self, beat_out, measure_number, beat_number, start_index):
        measure_nodes = []
        prev_measure = measure_number[start_index]
        measure_beats_start = 0
        measure_beats_end = 0
        num_beats = beat_out.shape[1]
        start_beat = beat_number[start_index]
        for beat_index in range(num_beats):
            current_beat = start_beat + beat_index
            current_note_index = beat_number.index(current_beat)

            if measure_number[current_note_index] > prev_measure:
                # new beat start
                measure_beats_end = beat_index
                corresp_hidden = beat_out[0, measure_beats_start:measure_beats_end, :]
                measure = self.sum_with_attention(corresp_hidden, self.measure_attention)
                measure_nodes.append(measure)

                measure_beats_start = beat_index
                prev_measure = measure_number[current_note_index]

        last_hidden = beat_out[0, measure_beats_end:, :]
        measure = self.sum_with_attention(last_hidden, self.measure_attention)
        measure_nodes.append(measure)

        measure_nodes = torch.stack(measure_nodes).view(1,-1,self.beat_hidden_size*2)

        return measure_nodes

    def span_beat_to_note_num(self, beat_out, beat_number, num_notes, start_index):
        start_beat = beat_number[start_index]
        num_beat = beat_out.shape[1]
        span_mat = torch.zeros(1, num_notes, num_beat)
        node_size = beat_out.shape[2]
        for i in range(num_notes):
            beat_index = beat_number[start_index+i] - start_beat
            span_mat[0,i,beat_index] = 1
        span_mat = span_mat.to(self.device)

        spanned_beat = torch.bmm(span_mat, beat_out)
        return spanned_beat

    def note_tempo_infos_to_beat(self, y, beat_numbers, start_index, index=None):
        beat_tempos = []
        num_notes = y.size(1)
        prev_beat = -1
        for i in range(num_notes):
            cur_beat = beat_numbers[start_index+i]
            if cur_beat > prev_beat:
                if index is None:
                    beat_tempos.append(y[0,i,:])
                else:
                    beat_tempos.append(y[0,i,index])
                prev_beat = cur_beat
        num_beats = len(beat_tempos)
        beat_tempos = torch.stack(beat_tempos).view(1,num_beats,-1)
        return beat_tempos

    def run_voice_net(self, batch_x, voice_hidden, voice_numbers, max_voice):
        num_notes = batch_x.size(1)
        # output = torch.zeros(1, batch_x.size(1), self.voice_hidden_size * 2).to(device)
        output = torch.zeros(1, batch_x.size(1), self.note_hidden_size * 2).to(self.device)
        voice_numbers = torch.Tensor(voice_numbers)
        for i in range(1,max_voice+1):
            voice_x_bool = voice_numbers == i
            num_voice_notes = torch.sum(voice_x_bool)
            if num_voice_notes > 0:
                span_mat = torch.zeros(num_notes, num_voice_notes)
                note_index_in_voice = 0
                for j in range(num_notes):
                    if voice_x_bool[j] ==1:
                        span_mat[j, note_index_in_voice] = 1
                        note_index_in_voice += 1
                span_mat = span_mat.view(1,num_notes,-1).to(self.device)
                voice_x = batch_x[0,voice_x_bool,:].view(1,-1, self.input_size)
                ith_hidden = voice_hidden[i-1]

                ith_voice_out, ith_hidden = self.voice_net(voice_x, ith_hidden)
                output += torch.bmm(span_mat, ith_voice_out)
        return output, voice_hidden

    def encode_with_net(self, score_input, mean_net, var_net):
        mu = mean_net(score_input)
        var = var_net(score_input)

        z = self.reparameterize(mu, var)
        return z, mu, var

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5*logvar)
        eps = torch.randn_like(std)
        return eps.mul(std).add_(mu)


    def init_hidden(self, batch_size):
        h0 = torch.zeros(self.note_layer_num * 2, batch_size, self.note_hidden_size).to(self.device)
        return (h0, h0)

    def init_final_layer(self, batch_size):
        h0 = torch.zeros(self.unidir_layer_num, batch_size, self.unidir_hidden_size).to(self.device)
        return (h0, h0)

    def init_beat_layer(self, batch_size):
        h0 = torch.zeros(self.beat_layer_num * 2, batch_size, self.beat_hidden_size).to(self.device)
        return (h0, h0)

    def init_measure_layer(self, batch_size):
        h0 = torch.zeros(self.measure_layer_num * 2, batch_size, self.measure_hidden_size).to(self.device)
        return (h0, h0)

    def init_beat_tempo_forward(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.beat_hidden_size).to(self.device)
        return (h0, h0)

    def init_voice_layer(self, batch_size, max_voice):
        layers = []
        for i in range(max_voice):
            # h0 = torch.zeros(self.num_voice_layers * 2, batch_size, self.voice_hidden_size).to(device)
            h0 = torch.zeros(self.note_layer_num * 2, batch_size, self.note_hidden_size).to(self.device)
            layers.append((h0,h0))
        return layers